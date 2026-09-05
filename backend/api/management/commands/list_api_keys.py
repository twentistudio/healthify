"""
Tampilkan kunci API Intelligence yang terdaftar.

Yang tampil hanya penanda dan beberapa karakter awal kunci. Nilai asli tidak
disimpan, jadi tidak bisa ditampilkan ulang oleh siapa pun.

Pemakaian:
    python manage.py list_api_keys
    python manage.py list_api_keys --consumer healthtalk
    python manage.py list_api_keys --all        # termasuk yang sudah dicabut
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from api.models import IntelligenceApiKey


class Command(BaseCommand):
    help = "Tampilkan kunci API Intelligence yang terdaftar."

    def add_arguments(self, parser):
        parser.add_argument("--consumer", default="", help="Saring berdasarkan konsumen.")
        parser.add_argument("--all", action="store_true",
                            help="Tampilkan juga kunci yang sudah dicabut.")

    def handle(self, *args, **options):
        queryset = IntelligenceApiKey.objects.all()
        if options["consumer"]:
            queryset = queryset.filter(consumer=options["consumer"].strip())
        if not options["all"]:
            queryset = queryset.filter(is_active=True)

        self.stdout.write(self.style.MIGRATE_HEADING("Kunci di database"))
        if not queryset.exists():
            self.stdout.write("  (belum ada)")
        for key in queryset:
            state = "aktif " if key.is_active else "dicabut"
            used = key.last_used_at.strftime("%Y-%m-%d %H:%M") if key.last_used_at else "belum pernah"
            label = f" · {key.label}" if key.label else ""
            rate = f" · {key.rate}" if key.rate else ""
            self.stdout.write(
                f"  #{key.id:<4} [{state}] {key.consumer}{label}{rate}\n"
                f"        awalan {key.key_prefix}…  dipakai terakhir: {used}"
            )

        env_keys = getattr(settings, "INTELLIGENCE_API_KEYS", None) or {}
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Kunci dari variabel lingkungan"))
        if not env_keys:
            self.stdout.write("  (tidak ada)")
        for raw, consumer in env_keys.items():
            self.stdout.write(f"  {consumer:<20} awalan {raw[:16]}…")
