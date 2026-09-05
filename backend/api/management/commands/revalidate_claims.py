"""
Selaraskan ulang hasil verifikasi lama dengan bukti yang benar-benar tersisa.

Latar
-----
`audit_source_links --fix` membuang DOI/URL yang terbukti tidak terdaftar.
Akibatnya sebagian klaim lama kini berlabel FAKTA/HOAX padahal SELURUH sumber
pendukungnya sudah tidak punya tautan yang bisa dipertanggungjawabkan —
label itu tidak lagi berdasar dan menyesatkan pembaca.

Aturan produk Healthify yang sudah ada berbunyi: tanpa sumber jurnal, label
adalah TIDAK TERVERIFIKASI (`unverified`, confidence NULL). Perintah ini
menerapkan aturan itu pada data yang kondisinya berubah.

Pemakaian:
    python manage.py revalidate_claims                 # laporan saja
    python manage.py revalidate_claims --fix           # turunkan ke unverified
    python manage.py revalidate_claims --fix --reverify  # verifikasi ulang penuh
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from api.models import Claim, VerificationResult


class Command(BaseCommand):
    help = ("Turunkan label klaim yang seluruh sumbernya kehilangan tautan "
            "menjadi 'unverified', atau verifikasi ulang.")

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="Terapkan perubahan. Tanpa ini hanya laporan.")
        parser.add_argument("--reverify", action="store_true",
                            help="Jalankan ulang verifikasi AI (butuh --fix). "
                                 "Tanpa ini label cukup diturunkan ke 'unverified'.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Batasi jumlah klaim yang diproses (0 = semua).")

    def handle(self, *args, **options):
        fix = options["fix"]
        reverify = options["reverify"]

        if reverify and not fix:
            self.stdout.write(self.style.ERROR("--reverify memerlukan --fix. Dibatalkan."))
            return

        self.stdout.write(self.style.WARNING(
            "Mode: " + ("PERBAIKI" + (" + VERIFIKASI ULANG" if reverify else "")
                        if fix else "DRY-RUN (tidak ada perubahan)")
        ))
        self.stdout.write("")

        affected = self._find_affected(options["limit"])

        if not affected:
            self.stdout.write(self.style.SUCCESS(
                "Tidak ada klaim yang labelnya kehilangan dasar bukti."
            ))
            return

        changed = 0
        for claim, verification, linked, total in affected:
            self.stdout.write(self.style.ERROR(
                f"  Klaim #{claim.id} [{verification.label}"
                f"{'' if verification.confidence is None else f' {verification.confidence:.2f}'}]"
                f" sumber berlink {linked}/{total} — {claim.text[:50]}"
            ))
            if not fix:
                continue

            if reverify:
                changed += int(self._reverify(claim, verification))
            else:
                verification.label = VerificationResult.LABEL_UNVERIFIED
                verification.confidence = None
                verification.reviewer_notes = (
                    (verification.reviewer_notes or "")
                    + "\n[sistem] Diturunkan ke TIDAK TERVERIFIKASI: seluruh sumber "
                      "pendukung tidak memiliki tautan yang dapat diverifikasi."
                ).strip()
                verification.save(update_fields=["label", "confidence",
                                                 "reviewer_notes", "updated_at"])
                changed += 1

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RINGKASAN"))
        self.stdout.write(f"  klaim tanpa dasar bukti : {len(affected)}")
        if fix:
            self.stdout.write(self.style.SUCCESS(f"  diperbarui              : {changed}"))
        else:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Jalankan ulang dengan --fix untuk menerapkan."
            ))

    # ------------------------------------------------------------------
    def _find_affected(self, limit):
        """Klaim berlabel selain 'unverified' yang tidak punya satu pun sumber bertautan."""
        queryset = (
            Claim.objects
            .select_related("verification_result")
            .annotate(
                total_sources=Count("claimsource", distinct=True),
                linked_sources=Count(
                    "claimsource",
                    filter=Q(claimsource__source__doi__isnull=False)
                    | Q(claimsource__source__url__isnull=False),
                    distinct=True,
                ),
            )
            .order_by("id")
        )
        if limit:
            queryset = queryset[:limit]

        affected = []
        for claim in queryset:
            verification = getattr(claim, "verification_result", None)
            if verification is None:
                continue
            if verification.label == VerificationResult.LABEL_UNVERIFIED:
                continue
            if claim.linked_sources == 0:
                affected.append((claim, verification, claim.linked_sources, claim.total_sources))
        return affected

    def _reverify(self, claim, verification) -> bool:
        from api.ai_adapter import call_ai_verify

        try:
            result = call_ai_verify(claim.text)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"    verifikasi ulang gagal untuk #{claim.id}: {str(exc)[:120]}"
            ))
            return False

        verification.label = result.get("label", "unverified")
        verification.confidence = result.get("confidence")
        verification.summary = result.get("summary", verification.summary)
        verification.save(update_fields=["label", "confidence", "summary", "updated_at"])
        self.stdout.write(self.style.SUCCESS(
            f"    -> {verification.label}"
            f"{'' if verification.confidence is None else f' {verification.confidence:.2f}'}"
        ))
        return True
