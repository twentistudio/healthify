"""
Penanda sitasi internal.

Model diminta menutup kalimatnya dengan penanda seperti `[E1]` supaya setiap
pernyataan dapat ditelusuri ke bukti nomor berapa. Penanda itu berguna di dalam
sistem, tetapi tidak pernah dimaksudkan terbaca oleh pengguna akhir: di layar,
ia muncul sebagai angka dalam kurung yang tidak berarti apa-apa bagi pembaca.

Satu definisi di sini dipakai seluruh jalur yang menghasilkan teks siap tampil,
supaya tidak ada satu jalur pun yang lupa membersihkannya.
"""

import re

CITATION_MARKER_RE = re.compile(r"\s*\[E\d{1,2}\]")


def strip_citation_markers(text: str) -> str:
    """Buang penanda sitasi dan rapikan spasi yang ditinggalkannya."""
    cleaned = CITATION_MARKER_RE.sub("", text or "")
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()
