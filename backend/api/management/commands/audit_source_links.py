"""
Audit tautan sumber yang SUDAH ada di database.

Dua hal diperiksa, dan keduanya berbeda:

1. **Apakah DOI/URL dapat dijangkau.** Menangkap tautan mati dan DOI karangan
   yang tidak pernah terdaftar.

2. **Apakah JUDUL tersimpan benar-benar milik DOI tersebut.** Ini pemeriksaan
   yang lebih dalam dan sama pentingnya. Judul yang meyakinkan bisa dipasangkan
   dengan DOI yang kebetulan nyata tetapi milik paper lain, sehingga pembaca
   membuka halaman yang sama sekali berbeda dari judul yang diklik. Pemeriksaan
   nomor 1 meloloskan kasus ini karena DOI-nya memang ada.

Registry adalah satu-satunya otoritas untuk judul sebuah DOI.

Pemakaian:
    python manage.py audit_source_links                 # laporan saja (dry-run)
    python manage.py audit_source_links --fix           # bersihkan & perbaiki judul
    python manage.py audit_source_links --fix --delete-orphans
    python manage.py audit_source_links --limit 200 --only sources
"""

import time

from django.core.management.base import BaseCommand

from api.intelligence.evidence import link_validator as lv
from api.models import JournalArticle, Source


class Command(BaseCommand):
    help = ("Verifikasi DOI/URL dan kecocokan judul pada Source & JournalArticle; "
            "laporkan atau perbaiki.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix", action="store_true",
            help="Kosongkan DOI/URL yang tidak dapat dijangkau dan ganti judul "
                 "yang tidak cocok dengan judul resmi dari registry. "
                 "Tanpa flag ini perintah hanya melaporkan (dry-run).",
        )
        parser.add_argument(
            "--delete-orphans", action="store_true",
            help="Hapus Source yang setelah pembersihan tidak punya DOI maupun URL "
                 "dan tidak tertaut ke klaim mana pun. Perlu --fix.",
        )
        parser.add_argument("--limit", type=int, default=0,
                            help="Batasi jumlah baris yang diperiksa (0 = semua).")
        parser.add_argument("--only", choices=["sources", "journals"], default=None,
                            help="Periksa satu tabel saja.")
        parser.add_argument("--delay", type=float, default=0.1,
                            help="Jeda antar permintaan registry, detik (default 0.1).")
        parser.add_argument("--skip-titles", action="store_true",
                            help="Lewati pemeriksaan judul (hanya cek tautan).")
        parser.add_argument(
            "--drop-mismatched", action="store_true",
            help="Untuk judul yang tidak cocok, BUANG DOI/URL-nya alih-alih "
                 "mengganti judul. Dipakai untuk membersihkan data karangan LLM: "
                 "mengganti judulnya hanya menghasilkan sitasi jujur yang tidak "
                 "relevan dengan klaimnya. Perlu --fix.")

    def handle(self, *args, **options):
        fix = options["fix"]
        delete_orphans = options["delete_orphans"]
        limit = options["limit"]
        only = options["only"]
        delay = options["delay"]
        self.check_titles = not options["skip_titles"]
        self.drop_mismatched = options["drop_mismatched"]

        if self.drop_mismatched and not fix:
            self.stdout.write(self.style.ERROR(
                "--drop-mismatched memerlukan --fix. Dibatalkan."
            ))
            return

        if delete_orphans and not fix:
            self.stdout.write(self.style.ERROR(
                "--delete-orphans memerlukan --fix. Dibatalkan."
            ))
            return

        mode = "PERBAIKI" if fix else "DRY-RUN (tidak ada perubahan)"
        self.stdout.write(self.style.WARNING(f"Mode: {mode}"))
        self.stdout.write("")

        totals = {"checked": 0, "verified": 0, "broken": 0, "unknown": 0,
                  "cleaned": 0, "deleted": 0, "mismatched": 0, "retitled": 0,
                  "dropped_mismatched": 0}

        if only != "journals":
            self._audit_sources(fix, delete_orphans, limit, delay, totals)
        if only != "sources":
            self._audit_journals(fix, limit, delay, totals)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RINGKASAN"))
        self.stdout.write(f"  diperiksa        : {totals['checked']}")
        self.stdout.write(self.style.SUCCESS(f"  tautan sah       : {totals['verified']}"))
        self.stdout.write(self.style.ERROR(f"  tautan rusak/404 : {totals['broken']}"))
        self.stdout.write(self.style.ERROR(f"  judul tidak cocok: {totals['mismatched']}"))
        self.stdout.write(f"  tidak terpastikan: {totals['unknown']}")
        if fix:
            self.stdout.write(self.style.SUCCESS(f"  tautan dibersihkan: {totals['cleaned']}"))
            self.stdout.write(self.style.SUCCESS(f"  judul diperbaiki  : {totals['retitled']}"))
            self.stdout.write(self.style.SUCCESS(
                f"  DOI dibuang (judul beda): {totals['dropped_mismatched']}"))
            self.stdout.write(self.style.SUCCESS(f"  baris dihapus     : {totals['deleted']}"))
        elif totals["broken"] or totals["mismatched"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Jalankan ulang dengan --fix untuk menerapkan perbaikan."
            ))

    # ------------------------------------------------------------------
    def _check(self, doi, url, delay):
        result = lv.validate_reference(doi or "", url or "", trust_on_unknown=False)
        if delay:
            time.sleep(delay)
        return result

    def _check_title(self, row, doi, label, fix, totals, delay):
        """Pastikan judul tersimpan benar-benar milik DOI tersebut."""
        if not self.check_titles or not doi:
            return

        metadata = lv.fetch_doi_metadata(doi)
        if delay:
            time.sleep(delay)

        registry_title = (metadata or {}).get("title", "").strip()
        if not registry_title:
            return

        stored = (row.title or "").strip()
        if lv.titles_match(stored, registry_title):
            return

        totals["mismatched"] += 1
        self.stdout.write(self.style.ERROR(
            f"  [judul beda] {label} #{row.id} doi={doi}"
        ))
        self.stdout.write(f"      tersimpan : {stored[:76]}")
        self.stdout.write(f"      registry  : {registry_title[:76]}")

        if not fix:
            return

        # Pasangan (judul, DOI) yang tidak cocok berarti keduanya tidak dapat
        # dipercaya mewakili bukti apa pun. Ada dua penanganan:
        #
        #   --drop-mismatched : buang DOI/URL-nya. Baris berhenti menjadi bukti,
        #       dan klaim yang kehilangan seluruh sumbernya akan diturunkan oleh
        #       `revalidate_claims`. Ini yang benar untuk data karangan LLM:
        #       mengganti judulnya hanya menghasilkan sitasi yang jujur tetapi
        #       tidak relevan dengan klaimnya.
        #
        #   default : ganti judul dengan judul resmi registry. Aman dan tidak
        #       merusak, tautan dan judul akhirnya sesuai.
        if self.drop_mismatched:
            row.doi = None
            row.url = None
            row.save(update_fields=["doi", "url", "updated_at"])
            totals["dropped_mismatched"] += 1
            self.stdout.write(self.style.WARNING(
                f"      -> DOI dibuang (pasangan judul/DOI tidak dapat dipercaya)"
            ))
            return

        max_title = 500 if label == "Source" else 1000
        row.title = registry_title[:max_title]
        fields = ["title", "updated_at"]

        publisher = (metadata.get("container") or metadata.get("publisher") or "").strip()
        if publisher:
            row.publisher = publisher[:255 if label == "Source" else 500]
            fields.append("publisher")

        if metadata.get("authors") and not (row.authors or "").strip():
            row.authors = metadata["authors"]
            fields.append("authors")

        row.save(update_fields=fields)
        totals["retitled"] += 1

    def _audit_sources(self, fix, delete_orphans, limit, delay, totals):
        from api.models import ClaimSource

        self.stdout.write(self.style.MIGRATE_HEADING("Source"))
        queryset = Source.objects.all().order_by("id")
        if limit:
            queryset = queryset[:limit]

        for source in queryset:
            if not source.doi and not source.url:
                continue
            totals["checked"] += 1
            result = self._check(source.doi, source.url, delay)
            status = result["link_status"]

            if status == lv.STATUS_VERIFIED:
                totals["verified"] += 1
                if fix and (source.doi != result["doi"] or source.url != result["url"]):
                    source.doi = result["doi"] or None
                    source.url = result["url"] or None
                    source.save(update_fields=["doi", "url", "updated_at"])
                self._check_title(source, result["doi"], "Source", fix, totals, delay)
                continue

            if status in (lv.STATUS_UNRESOLVABLE, lv.STATUS_MALFORMED):
                totals["broken"] += 1
                self.stdout.write(self.style.ERROR(
                    f"  [{status}] Source #{source.id} doi={source.doi!r} url={source.url!r}"
                    f" - {source.title[:56]}"
                ))
                if fix:
                    source.doi = None
                    source.url = None
                    source.save(update_fields=["doi", "url", "updated_at"])
                    totals["cleaned"] += 1
                    if delete_orphans and not ClaimSource.objects.filter(source=source).exists():
                        source.delete()
                        totals["deleted"] += 1
            else:
                totals["unknown"] += 1

    def _audit_journals(self, fix, limit, delay, totals):
        self.stdout.write(self.style.MIGRATE_HEADING("JournalArticle"))
        queryset = JournalArticle.objects.all().order_by("id")
        if limit:
            queryset = queryset[:limit]

        for journal in queryset:
            if not journal.doi and not journal.url:
                continue
            totals["checked"] += 1
            result = self._check(journal.doi, journal.url, delay)
            status = result["link_status"]

            if status == lv.STATUS_VERIFIED:
                totals["verified"] += 1
                if fix and (journal.doi != result["doi"] or journal.url != result["url"]):
                    journal.doi = result["doi"] or None
                    journal.url = result["url"] or None
                    journal.save(update_fields=["doi", "url", "updated_at"])
                self._check_title(journal, result["doi"], "JournalArticle", fix, totals, delay)
                continue

            if status in (lv.STATUS_UNRESOLVABLE, lv.STATUS_MALFORMED):
                totals["broken"] += 1
                self.stdout.write(self.style.ERROR(
                    f"  [{status}] JournalArticle #{journal.id} doi={journal.doi!r}"
                    f" - {journal.title[:56]}"
                ))
                if fix:
                    journal.doi = None
                    journal.url = None
                    journal.save(update_fields=["doi", "url", "updated_at"])
                    totals["cleaned"] += 1
            else:
                totals["unknown"] += 1
