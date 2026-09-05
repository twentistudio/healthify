"""
Halaman muka engine publik (ragai).

Dilayani Django, bukan SPA Healthify: domain publik hanya menampilkan engine
dan dokumentasinya, sementara produk Healthify tetap berada di domainnya
sendiri.
"""

import logging

from django.conf import settings
from django.shortcuts import render

logger = logging.getLogger(__name__)


def _public_base_url(request) -> str:
    """
    Alamat publik engine.

    Skema diambil dari header proxy karena TLS diterminasi di depan; tanpa itu
    contoh perintah di halaman ini akan tertulis http:// pada halaman https://.
    """
    configured = getattr(settings, "PUBLIC_API_BASE_URL", "")
    if configured.startswith(("http://", "https://")):
        return configured.rstrip("/")

    forwarded = (request.META.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip()
    scheme = forwarded if forwarded in ("http", "https") else (
        "https" if request.is_secure() else request.scheme)
    return f"{scheme}://{request.get_host()}"


def _journal_count() -> str:
    """
    Jumlah jurnal di knowledge base, dibulatkan ke bawah ke ratusan terdekat.

    Angka pastinya berubah setiap kali engine melengkapi dirinya sendiri, dan
    menampilkan angka yang bergerak-gerak di halaman muka justru terbaca seperti
    hitungan yang dikarang. Pembulatan menjaga klaimnya tetap benar.
    """
    try:
        from .models import JournalArticle

        total = JournalArticle.objects.count()
    except Exception as exc:  # pragma: no cover - basis data belum siap
        logger.warning("[LANDING] gagal menghitung jurnal: %s", exc)
        return "peer reviewed"

    if total < 100:
        return f"{total}"
    return f"{total // 100 * 100:,}+".replace(",", ",")


def landing(request):
    """Halaman perkenalan engine."""
    return render(request, "landing.html", {
        "base_url": _public_base_url(request),
        "journal_count": _journal_count(),
    })
