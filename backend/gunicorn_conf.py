"""
Konfigurasi gunicorn.

Ada satu hal yang diurus di sini: menyembunyikan permintaan cek kesehatan dari
log akses. Docker memanggil `/api/health/` setiap tiga puluh detik selamanya,
dan baris-baris itu memenuhi hampir separuh isi log tanpa pernah memberi tahu
apa pun. Yang tersisa setelah disaring adalah lalu lintas sungguhan, sehingga
log kembali bisa dibaca ketika ada masalah.

Permintaan cek kesehatan yang GAGAL tetap dicatat: itu justru kejadian yang
perlu terlihat.
"""

import os


class SkipSuccessfulHealthChecks:
    """Buang baris log akses untuk cek kesehatan yang berhasil."""

    def filter(self, record):
        message = record.getMessage()
        if "/api/health/" not in message:
            return True
        # Format akses gunicorn memuat status setelah protokol.
        return ' 200 ' not in message


bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv('GUNICORN_WORKERS', '2'))
timeout = int(os.getenv('GUNICORN_TIMEOUT', '180'))
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = "info"

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "skip_health_checks": {
            "()": "gunicorn_conf.SkipSuccessfulHealthChecks",
        },
    },
    "formatters": {
        "generic": {
            "format": "%(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "generic",
            "stream": "ext://sys.stdout",
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "generic",
            "stream": "ext://sys.stdout",
            "filters": ["skip_health_checks"],
        },
    },
    "loggers": {
        "gunicorn.error": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "gunicorn.access": {
            "handlers": ["access"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
