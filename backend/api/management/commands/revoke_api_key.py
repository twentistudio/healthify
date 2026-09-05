"""
Cabut kunci API Intelligence.

Pencabutan bersifat menandai, bukan menghapus: baris tetap ada sebagai jejak
kapan kunci pernah berlaku dan kapan dicabut.

Pemakaian:
    python manage.py revoke_api_key --id 3
    python manage.py revoke_api_key --consumer healthtalk --all
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import IntelligenceApiKey


class Command(BaseCommand):
    help = "Cabut satu kunci API Intelligence, atau seluruh kunci satu konsumen."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, default=None, help="ID kunci yang dicabut.")
        parser.add_argument("--consumer", default="", help="Nama konsumen.")
        parser.add_argument("--all", action="store_true",
                            help="Cabut SELURUH kunci aktif milik konsumen tersebut.")

    def handle(self, *args, **options):
        if options["id"] is not None:
            queryset = IntelligenceApiKey.objects.filter(pk=options["id"], is_active=True)
        elif options["consumer"] and options["all"]:
            queryset = IntelligenceApiKey.objects.filter(
                consumer=options["consumer"].strip(), is_active=True)
        else:
            self.stdout.write(self.style.ERROR(
                "Sebutkan --id, atau --consumer bersama --all. Dibatalkan."))
            return

        count = queryset.count()
        if not count:
            self.stdout.write("Tidak ada kunci aktif yang cocok.")
            return

        queryset.update(is_active=False, revoked_at=timezone.now())
        self.stdout.write(self.style.SUCCESS(f"{count} kunci dicabut."))
