"""
Terbitkan kunci API Intelligence untuk satu konsumen.

Kunci dibuat dengan generator acak kriptografis, ditampilkan SEKALI, lalu hanya
SHA-256-nya yang disimpan. Tidak ada cara menampilkan ulang nilai aslinya; bila
hilang, terbitkan yang baru dan cabut yang lama.

Satu konsumen boleh memegang banyak kunci sekaligus, misalnya satu per
lingkungan atau per aplikasi, sehingga satu kunci dapat dicabut tanpa
mematikan yang lain.

Pemakaian:
    python manage.py issue_api_key --consumer healthtalk
    python manage.py issue_api_key --consumer healthtalk --label "backend produksi"
    python manage.py issue_api_key --consumer healthtalk --rate "120/min"
    python manage.py issue_api_key --consumer healthtalk --request 7
"""

import secrets

from django.core.management.base import BaseCommand

from api.models import ApiAccessRequest, IntelligenceApiKey

KEY_PREFIX = "ht_live_"
KEY_BYTES = 32


class Command(BaseCommand):
    help = "Terbitkan kunci API Intelligence baru untuk sebuah konsumen."

    def add_arguments(self, parser):
        parser.add_argument("--consumer", required=True,
                            help="Nama konsumen, mis. 'healthtalk'. Muncul di log dan kuota.")
        parser.add_argument("--label", default="",
                            help="Penanda bebas, mis. 'backend produksi'.")
        parser.add_argument("--rate", default="",
                            help="Batas laju khusus, mis. '120/min'. Kosong = bawaan.")
        parser.add_argument("--request", type=int, default=None,
                            help="ID ApiAccessRequest yang ditautkan, sekaligus menandainya disetujui.")

    def handle(self, *args, **options):
        consumer = options["consumer"].strip()
        if not consumer:
            self.stdout.write(self.style.ERROR("Nama konsumen tidak boleh kosong."))
            return

        access_request = None
        if options["request"] is not None:
            try:
                access_request = ApiAccessRequest.objects.get(pk=options["request"])
            except ApiAccessRequest.DoesNotExist:
                self.stdout.write(self.style.ERROR(
                    f"Permintaan #{options['request']} tidak ditemukan."))
                return

        raw_key = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)

        key = IntelligenceApiKey.objects.create(
            consumer=consumer,
            label=options["label"].strip(),
            key_hash=IntelligenceApiKey.hash_key(raw_key),
            key_prefix=raw_key[:16],
            rate=options["rate"].strip(),
            request_ref=access_request,
        )

        if access_request and access_request.status != ApiAccessRequest.STATUS_APPROVED:
            access_request.status = ApiAccessRequest.STATUS_APPROVED
            access_request.save(update_fields=["status", "updated_at"])

        existing = IntelligenceApiKey.objects.filter(
            consumer=consumer, is_active=True).count()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Kunci diterbitkan."))
        self.stdout.write(f"  konsumen : {consumer}")
        if key.label:
            self.stdout.write(f"  label    : {key.label}")
        if key.rate:
            self.stdout.write(f"  batas    : {key.rate}")
        self.stdout.write(f"  id       : {key.id}")
        self.stdout.write(f"  kunci aktif milik konsumen ini: {existing}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("  Salin sekarang, nilai ini tidak akan ditampilkan lagi:"))
        self.stdout.write("")
        self.stdout.write(f"    {raw_key}")
        self.stdout.write("")
        self.stdout.write("  Pemakaian oleh konsumen:")
        self.stdout.write("    X-API-Key: <kunci di atas>")
        self.stdout.write("")
        self.stdout.write("  Simpan di variabel lingkungan sisi server milik konsumen, "
                          "jangan di kode frontend.")
