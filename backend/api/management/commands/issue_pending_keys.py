"""
Terbitkan dan kirimkan kunci API untuk permintaan yang belum dilayani.

Aman dijalankan berulang: penentunya ada-tidaknya kunci yang pernah
diterbitkan, bukan status permintaan. Bila surat gagal terkirim, kunci yang
baru dibuat langsung dicabut agar tidak ada kunci hidup tanpa pemilik.

    python manage.py issue_pending_keys --dry-run
    python manage.py issue_pending_keys
"""

import re
import secrets

from django.core.management.base import BaseCommand

from api.models import ApiAccessRequest, IntelligenceApiKey

KEY_PREFIX = "ht_live_"
KEY_BYTES = 32


class Command(BaseCommand):
    help = ("Terbitkan kunci API untuk setiap permintaan akses yang belum "
            "pernah diberi kunci, lalu kirimkan ke alamat pemohon.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Tampilkan yang akan dikerjakan tanpa menerbitkan "
                                 "atau mengirim apa pun.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Batasi jumlah permintaan yang dilayani (0 = semua).")
        parser.add_argument("--rate", default="",
                            help="Batas laju khusus untuk kunci yang diterbitkan, "
                                 "mis. '120/min'. Kosong = batas bawaan.")
        parser.add_argument("--include-rejected", action="store_true",
                            help="Ikutkan permintaan yang sudah ditandai ditolak. "
                                 "Bawaannya dilewati.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        pending = self._pending_requests(options["include_rejected"])
        if options["limit"]:
            pending = pending[:options["limit"]]

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "Mode: DRY-RUN. Tidak ada kunci yang diterbitkan atau dikirim."))
            self.stdout.write("")

        if not pending:
            self.stdout.write("Tidak ada permintaan yang menunggu. Semua sudah dilayani.")
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{len(pending)} permintaan menunggu"))
        self.stdout.write("")

        issued = failed = 0
        for access_request in pending:
            consumer = self._consumer_name(access_request)
            label = (access_request.organization or "").strip()[:200]

            self.stdout.write(
                f"  #{access_request.id} {access_request.email} "
                f"-> konsumen '{consumer}'"
            )
            if dry_run:
                continue

            if self._serve(access_request, consumer, label, options["rate"].strip()):
                issued += 1
            else:
                failed += 1

        self.stdout.write("")
        if dry_run:
            self.stdout.write("Jalankan tanpa --dry-run untuk benar-benar menerbitkan.")
            return

        self.stdout.write(self.style.SUCCESS(f"  terkirim : {issued}"))
        if failed:
            self.stdout.write(self.style.ERROR(f"  gagal    : {failed}"))
            self.stdout.write(
                "  Kunci untuk yang gagal sudah dicabut, jadi tidak ada kunci hidup "
                "yang tidak dipegang siapa pun. Perbaiki pengiriman surel lalu "
                "jalankan perintah ini lagi.")

    def _pending_requests(self, include_rejected):
        """
        Permintaan yang belum pernah diberi kunci.

        Diperiksa dari relasi kunci, bukan kolom status: kunci yang pernah
        dicabut tetap berarti permintaan itu sudah dilayani.
        """
        queryset = ApiAccessRequest.objects.filter(issued_keys__isnull=True)
        if not include_rejected:
            queryset = queryset.exclude(status=ApiAccessRequest.STATUS_REJECTED)
        return list(queryset.order_by("created_at"))

    def _consumer_name(self, access_request) -> str:
        """
        Nama konsumen yang terbaca manusia dan unik.

        Dipakai di log dan kuota, jadi diambil dari nama organisasi atau bagian
        awal surel. Id permintaan ditambahkan bila namanya sudah dipakai.
        """
        raw = (access_request.organization or "").strip()
        if not raw:
            raw = (access_request.email or "").split("@")[0]

        slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:40] or "consumer"
        if IntelligenceApiKey.objects.filter(consumer=slug).exists():
            slug = f"{slug}-{access_request.id}"
        return slug

    def _serve(self, access_request, consumer, label, rate) -> bool:
        """Terbitkan satu kunci dan kirimkan. Mengembalikan True bila surat terkirim."""
        from api.email_service import email_service

        raw_key = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
        key = IntelligenceApiKey.objects.create(
            consumer=consumer,
            label=label,
            key_hash=IntelligenceApiKey.hash_key(raw_key),
            key_prefix=raw_key[:16],
            rate=rate,
            request_ref=access_request,
        )

        sent = False
        try:
            sent = email_service.send_api_key(
                recipient=access_request.email,
                api_key=raw_key,
                name=access_request.name,
                label=label,
                rate=rate,
            )
        except Exception as exc:  # pragma: no cover - jaring pengaman
            self.stdout.write(self.style.ERROR(f"      kesalahan pengiriman: {exc}"))

        if not sent:
            # Kunci ini tidak pernah sampai ke siapa pun. Dibiarkan aktif, ia
            # hanya menjadi kredensial hidup tanpa pemilik.
            key.is_active = False
            key.save(update_fields=["is_active"])
            self.stdout.write(self.style.ERROR(
                "      GAGAL dikirim; kunci dicabut kembali."))
            return False

        if access_request.status != ApiAccessRequest.STATUS_APPROVED:
            access_request.status = ApiAccessRequest.STATUS_APPROVED
            access_request.save(update_fields=["status", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"      terkirim ke {access_request.email} (kunci #{key.id})"))
        return True
