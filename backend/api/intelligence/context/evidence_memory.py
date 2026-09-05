"""
Ingatan bukti untuk satu percakapan (§9, §10).

Kenapa modul ini ada
--------------------
Di dalam sebuah ruang obrolan, satu topik dibahas lewat banyak gelembung
pesan. Bila setiap pertanyaan lanjutan mengulang pencarian dari nol, jurnal
yang terpilih bisa berganti-ganti antar giliran, dan pengguna melihat jawaban
yang seolah berubah pendirian untuk pembahasan yang sama. Padahal yang berubah
hanya urutan pencarian.

Aturannya mengikuti alur percakapan yang wajar:

1. Pertanyaan pertama selalu mencari jurnal lewat RAG.
2. Pertanyaan berikutnya diperiksa dulu. Bila masih terjawab oleh jurnal yang
   sudah dipakai di percakapan ini, jurnal itulah yang dipakai kembali —
   jawabannya tetap bersandar pada rujukan yang sama.
3. Bila pertanyaannya sudah di luar jangkauan jurnal tersebut, pencarian
   dijalankan lagi.

Yang disimpan hanyalah pengenal barisnya (`journal:12`, `source:34`), bukan
salinan isinya, sehingga judul dan metadata selalu dibaca ulang dari sumber
aslinya dan tidak pernah basi.
"""

import json
import logging
import re
from typing import List, Optional

from ..contracts import EvidenceItem, EvidenceOrigin

logger = logging.getLogger(__name__)

# Berapa giliran ke belakang yang diingat. Cukup panjang untuk menjaga satu
# pembahasan tetap konsisten, cukup pendek agar topik yang sudah lama
# ditinggalkan tidak ikut menempel pada pertanyaan baru.
MEMORY_TURNS = 6
MAX_POOL = 12


def recent_evidence(session, limit: int = MAX_POOL) -> List[EvidenceItem]:
    """
    Bukti yang dipakai pada giliran-giliran terakhir percakapan ini.

    Dibaca ulang dari knowledge base memakai pengenal yang tersimpan, bukan
    dari salinan di riwayat, supaya judul yang sudah diperbaiki lewat registry
    ikut terbawa.
    """
    if session is None:
        return []

    source_ids: List[str] = []
    try:
        # Giliran TERBARU lebih dulu. Urutan bawaan pesan menaik, sehingga
        # memotong dari depan justru mengambil giliran paling lama: setelah
        # pembahasan berpindah, lanjutan berikutnya akan memakai jurnal topik
        # yang sudah ditinggalkan.
        messages = list(
            session.messages.filter(role="assistant").order_by("-created_at", "-id")[:MEMORY_TURNS]
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("[MEMORY] gagal membaca riwayat: %s", exc)
        return []

    for message in messages:
        if not message.evidence_refs:
            continue
        try:
            refs = json.loads(message.evidence_refs)
        except (ValueError, TypeError):
            continue
        for ref in refs or []:
            source_id = (ref or {}).get("source_id")
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)

    return _load_items(source_ids[:limit])


def _load_items(source_ids: List[str]) -> List[EvidenceItem]:
    """Bangun ulang EvidenceItem dari pengenal baris knowledge base."""
    if not source_ids:
        return []

    journal_ids, plain_ids = [], []
    for source_id in source_ids:
        kind, _, raw = source_id.partition(":")
        if not raw.isdigit():
            continue
        (journal_ids if kind == "journal" else plain_ids).append(int(raw))

    items: List[EvidenceItem] = []
    order = {sid: i for i, sid in enumerate(source_ids)}

    try:
        from ...models import JournalArticle, Source

        if journal_ids:
            for row in JournalArticle.objects.filter(id__in=journal_ids):
                items.append(_from_journal(row))
        if plain_ids:
            for row in Source.objects.filter(id__in=plain_ids):
                items.append(_from_source(row))
    except Exception as exc:  # pragma: no cover
        logger.warning("[MEMORY] gagal memuat bukti tersimpan: %s", exc)
        return []

    items.sort(key=lambda item: order.get(item.source_id, 999))
    return items


def _from_journal(row) -> EvidenceItem:
    return EvidenceItem(
        chunk_id=f"journal:{row.id}",
        source_id=f"journal:{row.id}",
        title=(row.title or "").strip(),
        snippet=(row.abstract or "").strip()[:1200],
        doi=(row.doi or "").strip(),
        url=(row.url or "").strip(),
        authors=(row.authors or "").strip(),
        publisher=(row.publisher or row.journal_name or "").strip(),
        published_year=row.published_date.year if row.published_date else None,
        source_type="journal",
        origin=EvidenceOrigin.KNOWLEDGE_BASE,
    )


def _from_source(row) -> EvidenceItem:
    return EvidenceItem(
        chunk_id=f"source:{row.id}",
        source_id=f"source:{row.id}",
        title=(row.title or "").strip(),
        snippet=(getattr(row, "excerpt", "") or "").strip()[:1200],
        doi=(row.doi or "").strip(),
        url=(row.url or "").strip(),
        authors=(getattr(row, "authors", "") or "").strip(),
        publisher=(row.publisher or "").strip(),
        source_type="source",
        origin=EvidenceOrigin.KNOWLEDGE_BASE,
    )


def can_answer_from_memory(query: str, pooled: List[EvidenceItem]) -> bool:
    """
    Apakah pertanyaan ini masih terjawab oleh jurnal yang sudah dipakai.

    Yang diperiksa adalah pertanyaan ASLI pengguna, bukan query yang sudah
    diperkaya konteks percakapan. Query yang diperkaya sengaja membawa topik
    sebelumnya, sehingga memakainya di sini selalu menghasilkan jawaban "masih
    tercakup" dan percakapan tidak pernah bisa berpindah topik.

    Dua keadaan dibedakan:

    * Pertanyaan menyebut penyakit. Itu penentunya: bila penyakit tersebut
      tidak dibahas jurnal yang ada, pembahasan sudah berpindah dan pencarian
      baru harus dijalankan, meskipun kalimatnya masih di ruang obrolan yang
      sama ("kalau asam urat bagaimana").

    * Pertanyaan tidak menyebut penyakit apa pun. Ini lanjutan wajar dari
      pembahasan berjalan ("apa gejalanya", "berapa lama sembuhnya"), dan
      dijawab dari jurnal yang sama. Pengecualiannya: bila pertanyaan memuat
      kata yang tidak dikenali kosakata kesehatan dan juga tidak muncul di
      jurnal tersebut, kemungkinan besar itu penyakit yang belum tercatat di
      leksikon, jadi diperlakukan sebagai topik baru.
    """
    if not pooled:
        return False

    try:
        from ..lexicon import bilingual_variants
        from ..retrieval.acquisition import _QUERY_NOISE
        from ..retrieval.concepts import extract_conditions, extract_health_concepts
    except Exception as exc:  # pragma: no cover
        logger.warning("[MEMORY] pemeriksaan cakupan gagal: %s", exc)
        return False

    haystack = " ".join(
        f"{item.title or ''} {item.snippet or ''}" for item in pooled
    ).lower()
    if not haystack.strip():
        return False

    def mentioned(term: str) -> bool:
        forms = {term.lower()} | {v.lower() for v in bilingual_variants(term)}
        return any(form in haystack for form in forms)

    subjects = extract_conditions(query) or []
    if subjects:
        return any(mentioned(subject) for subject in subjects)

    known = {c.lower() for c in (extract_health_concepts(query) or [])}
    tokens = [w for w in re.findall(r"[a-z0-9]{5,}", (query or "").lower())
              if w not in _QUERY_NOISE]
    unknown = [w for w in tokens
               if not any(w in term or term in w for term in known)]
    if unknown and not any(mentioned(word) for word in unknown):
        return False

    return True


def topic_changed(query: str, pooled: List[EvidenceItem]) -> bool:
    """
    Apakah pertanyaan ini membuka penyakit lain dari yang sedang dibahas.

    Dibedakan dari sekadar "tidak terjawab oleh jurnal yang ada": di sini
    pengguna menyebut penyakit tertentu, dan penyakit itu bukan yang dibahas
    jurnal percakapan. Pada keadaan itu konteks percakapan sebelumnya justru
    menyesatkan pencarian — istilah topik lama menenggelamkan penyakit yang
    baru disebut, sehingga pencarian "baru" mengembalikan jurnal yang sama.
    """
    if not pooled:
        return False

    try:
        from ..lexicon import bilingual_variants
        from ..retrieval.concepts import extract_conditions
    except Exception:  # pragma: no cover
        return False

    subjects = extract_conditions(query) or []
    if not subjects:
        return False

    haystack = " ".join(
        f"{item.title or ''} {item.snippet or ''}" for item in pooled
    ).lower()

    for subject in subjects:
        forms = {subject.lower()} | {v.lower() for v in bilingual_variants(subject)}
        if any(form in haystack for form in forms):
            return False
    return True
