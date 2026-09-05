"""
Isi knowledge base dengan artikel jurnal dari Crossref.

Setiap DOI diverifikasi ke registry sebelum disimpan, sehingga tidak ada tautan
404 yang masuk. Tanpa isi, engine selalu menjawab bukti tidak cukup.

    python manage.py import_journals --query "demam berdarah" --rows 25
    python manage.py import_journals --topics-id --embed
"""

import time
from typing import Any, Dict, List, Optional

import requests
from django.core.management.base import BaseCommand

from ragai.evidence import link_validator as lv
from api.models import JournalArticle

CROSSREF_API = "https://api.crossref.org/works"

# Topik kesehatan yang paling sering ditanyakan pengguna Indonesia.
DEFAULT_TOPICS_ID = [
    # Penyakit menular yang sering ditanyakan
    "dengue fever management",
    "dengue hemorrhagic fever severity",
    "typhoid fever diagnosis",
    "tuberculosis treatment adherence",
    "tuberculosis transmission airborne",
    "COVID-19 severity risk factors",
    "COVID-19 complications mortality",
    "COVID-19 transmission prevention",
    "COVID-19 vaccine effectiveness safety",
    "influenza illness burden adults",
    "common cold acute cough management",
    "chronic cough causes evaluation",
    "acute respiratory infection children",
    "diarrhea management children",
    "hepatitis B infection management",
    "HIV antiretroviral therapy adherence",
    "antibiotic resistance rational use",
    "urinary tract infection antibiotic",
    "skin bacterial infection treatment",

    # Penyakit tidak menular
    "hypertension lifestyle intervention",
    "hypertension complications organ damage",
    "type 2 diabetes management",
    "hyperglycemia complications diabetes",
    "dyslipidemia cholesterol management",
    "gout hyperuricemia diet purine",
    "stroke risk factors prevention",
    "coronary heart disease prevention",
    "chronic kidney disease progression",
    "asthma control inhaled therapy",
    "gastroesophageal reflux disease treatment",
    "dyspepsia gastritis management",
    "peptic ulcer Helicobacter pylori",
    "migraine headache management",
    "low back pain management",
    "osteoporosis fracture prevention",
    "osteoarthritis knee management",
    "thyroid disorder management",
    "cancer screening early detection",
    "smoking cessation lung cancer risk",

    # Gizi, gaya hidup, kesehatan umum
    "water intake hydration health",
    "dehydration fluid balance adults",
    "hand hygiene infection prevention",
    "dietary salt intake blood pressure",
    "added sugar intake health outcomes",
    "dietary fiber health outcomes",
    "vitamin D supplementation",
    "vitamin C supplementation common cold",
    "obesity physical activity intervention",
    "physical exercise cardiovascular benefit",
    "sleep duration health outcomes",
    "alcohol consumption health risk",
    "herbal medicine safety efficacy",

    # Ibu, anak, dan kesehatan jiwa
    "anemia iron deficiency",
    "stunting child nutrition",
    "maternal health pregnancy complications",
    "exclusive breastfeeding benefits infant",
    "childhood immunization coverage safety",
    "vaccine autism evidence",
    "depression treatment adults",
    "anxiety disorder management",
]

# Jenis publikasi yang diterima (hindari editorial/berita).
ACCEPTED_TYPES = {"journal-article", "proceedings-article", "book-chapter", "posted-content"}

MIN_ABSTRACT_CHARS = 200


class Command(BaseCommand):
    help = "Impor artikel jurnal nyata dari Crossref ke knowledge base (DOI diverifikasi)."

    def add_arguments(self, parser):
        parser.add_argument("--query", action="append", default=[],
                            help="Topik pencarian. Boleh diulang.")
        parser.add_argument("--topics-id", action="store_true",
                            help="Pakai daftar topik kesehatan umum Indonesia bawaan.")
        parser.add_argument("--rows", type=int, default=15,
                            help="Jumlah hasil per topik (default 15, maks 100).")
        parser.add_argument("--from-year", type=int, default=2015,
                            help="Hanya artikel terbit sejak tahun ini (default 2015).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Tampilkan yang akan diimpor tanpa menyimpan.")
        parser.add_argument("--embed", action="store_true",
                            help="Coba buat embedding setelah impor (butuh pipeline training).")
        parser.add_argument("--mailto", default=None,
                            help="Email untuk 'polite pool' Crossref (default: env CROSSREF_MAILTO).")

    def handle(self, *args, **options):
        queries: List[str] = list(options["query"])
        if options["topics_id"]:
            queries.extend(DEFAULT_TOPICS_ID)
        if not queries:
            self.stdout.write(self.style.ERROR(
                "Tidak ada topik. Pakai --query \"...\" atau --topics-id."
            ))
            return

        rows = max(1, min(options["rows"], 100))
        dry_run = options["dry_run"]

        import os
        mailto = options["mailto"] or os.getenv("CROSSREF_MAILTO", "")

        if dry_run:
            self.stdout.write(self.style.WARNING("Mode: DRY-RUN (tidak menyimpan)"))
        self.stdout.write("")

        stats = {"fetched": 0, "rejected_type": 0, "rejected_abstract": 0,
                 "rejected_doi": 0, "duplicate": 0, "created": 0}

        for query in queries:
            self.stdout.write(self.style.MIGRATE_HEADING(f"Topik: {query}"))
            try:
                items = self._search_crossref(query, rows, options["from_year"], mailto)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  gagal mengambil dari Crossref: {exc}"))
                continue

            stats["fetched"] += len(items)
            for item in items:
                self._process_item(item, dry_run, stats)
            time.sleep(0.5)  # sopan terhadap Crossref

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RINGKASAN"))
        self.stdout.write(f"  diambil dari Crossref : {stats['fetched']}")
        self.stdout.write(f"  ditolak (jenis)       : {stats['rejected_type']}")
        self.stdout.write(f"  ditolak (abstrak)     : {stats['rejected_abstract']}")
        self.stdout.write(f"  ditolak (DOI)         : {stats['rejected_doi']}")
        self.stdout.write(f"  duplikat              : {stats['duplicate']}")
        self.stdout.write(self.style.SUCCESS(f"  disimpan              : {stats['created']}"))
        self.stdout.write("")
        self.stdout.write(f"  total knowledge base  : {JournalArticle.objects.count()}")

        if options["embed"] and not dry_run and stats["created"]:
            self._embed_pending()

    def _search_crossref(self, query: str, rows: int, from_year: int,
                         mailto: str) -> List[Dict[str, Any]]:
        params = {
            "query.bibliographic": query,
            "rows": rows,
            "filter": f"from-pub-date:{from_year}-01-01,has-abstract:true",
            "select": "DOI,title,abstract,author,container-title,publisher,issued,type,URL,subject",
            "sort": "relevance",
        }
        if mailto:
            params["mailto"] = mailto

        response = requests.get(
            CROSSREF_API, params=params, timeout=30,
            headers={"User-Agent": f"Healthify/1.0 (mailto:{mailto})"},
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("items", []) or []

    def _process_item(self, item: Dict[str, Any], dry_run: bool, stats: Dict[str, int]):
        if item.get("type") not in ACCEPTED_TYPES:
            stats["rejected_type"] += 1
            return

        doi = lv.normalize_doi(item.get("DOI"))
        title = self._first(item.get("title"))
        abstract = self._clean_abstract(item.get("abstract"))

        if not doi or not title:
            stats["rejected_doi"] += 1
            return
        if len(abstract) < MIN_ABSTRACT_CHARS:
            stats["rejected_abstract"] += 1
            return

        if JournalArticle.objects.filter(doi=doi).exists():
            stats["duplicate"] += 1
            return

        # Verifikasi DOI ke registry sebelum baris disimpan.
        status = lv.resolve_doi(doi)
        if status != lv.STATUS_VERIFIED:
            stats["rejected_doi"] += 1
            self.stdout.write(self.style.ERROR(f"  DOI ditolak ({status}): {doi}"))
            return

        record = {
            "title": title[:1000],
            "abstract": abstract,
            "authors": self._authors(item.get("author")),
            "doi": doi,
            "url": lv.doi_to_url(doi),
            "publisher": (item.get("publisher") or "")[:500],
            "journal_name": (self._first(item.get("container-title")) or "")[:500],
            "published_date": self._issued_date(item.get("issued")),
            "source_portal": "other",
            "keywords": ", ".join((item.get("subject") or [])[:8]),
        }

        if dry_run:
            self.stdout.write(f"  [dry-run] {doi}, {title[:70]}")
            stats["created"] += 1
            return

        JournalArticle.objects.create(**record)
        stats["created"] += 1
        self.stdout.write(self.style.SUCCESS(f"  + {doi}, {title[:70]}"))

    @staticmethod
    def _first(value) -> str:
        if isinstance(value, list):
            return str(value[0]).strip() if value else ""
        return str(value or "").strip()

    @staticmethod
    def _clean_abstract(raw) -> str:
        """Crossref mengembalikan abstrak dalam JATS XML; buang tag-nya."""
        import html
        import re

        text = str(raw or "")
        text = re.sub(r"</?jats:[^>]+>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if text.lower().startswith("abstract"):
            text = text[len("abstract"):].lstrip(": ").strip()
        return text

    @staticmethod
    def _authors(authors) -> str:
        if not isinstance(authors, list):
            return ""
        names = []
        for author in authors[:12]:
            given = (author.get("given") or "").strip()
            family = (author.get("family") or "").strip()
            full = f"{family}, {given}".strip(", ") if family else given
            if full:
                names.append(full)
        return "; ".join(names)

    @staticmethod
    def _issued_date(issued) -> Optional[Any]:
        import datetime

        try:
            parts = (issued or {}).get("date-parts") or [[]]
            values = parts[0]
            if not values:
                return None
            year = int(values[0])
            month = int(values[1]) if len(values) > 1 else 1
            day = int(values[2]) if len(values) > 2 else 1
            return datetime.date(year, month, day)
        except Exception:
            return None

    def _embed_pending(self):
        from api.views import embed_journal_article

        # Seluruh yang tertunda, bukan 50 pertama. Jurnal tanpa embedding hanya
        # terjangkau lewat kecocokan kata kunci dan akan tersaring oleh lantai
        # kemiripan makna, sehingga impor besar yang berhenti di 50 embedding
        # menyisakan ribuan jurnal yang tidak pernah muncul sebagai referensi.
        pending = JournalArticle.objects.filter(is_embedded=False)
        total_pending = pending.count()
        if total_pending:
            self.stdout.write(f"  membuat embedding untuk {total_pending} jurnal...")
        embedded, failed = 0, 0
        for journal in pending:
            try:
                embed_journal_article(journal)
                embedded += 1
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f"  embed gagal untuk #{journal.id}: {str(exc)[:100]}"
                ))
        self.stdout.write(self.style.SUCCESS(f"  embedding dibuat: {embedded} (gagal: {failed})"))
        if failed:
            self.stdout.write(
                "  Catatan: embedding butuh pipeline training + pgvector. "
                "Tanpa itu retrieval tetap jalan memakai pencocokan leksikal."
            )
