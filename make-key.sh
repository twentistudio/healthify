#!/usr/bin/env bash
#
# Terbitkan dan kirimkan kunci API untuk permintaan akses yang belum dilayani.
#
# Mencari permintaan yang masuk lewat formulir di ragai.twenti.studio dan belum
# pernah diberi kunci, menerbitkan satu untuk masing-masing, lalu mengirimkannya
# ke alamat yang mereka isi.
#
# Pemakaian:
#   ./make-key.sh --dry-run          lihat siapa saja yang akan dilayani
#   ./make-key.sh                    terbitkan dan kirim
#   ./make-key.sh --limit 5          layani lima permintaan terlama saja
#   ./make-key.sh --rate 120/min     beri batas laju khusus
#
# Aman dijalankan berulang. Yang menentukan bukan status permintaan melainkan
# ada-tidaknya kunci yang pernah diterbitkan untuknya, sehingga menjalankan ini
# dua kali tidak menghasilkan kunci ganda, dan kunci yang sengaja dicabut tidak
# akan terbit lagi diam-diam.
#
# Kerja sesungguhnya ada di perintah Django `issue_pending_keys`. Berkas ini
# sengaja tipis: logika yang menyentuh basis data dan mengirim surat perlu bisa
# diuji, dan itu tidak mungkin bila ditulis sebagai rangkaian perintah bash.
#
# Menjalankannya terjadwal, misalnya tiap sepuluh menit:
#   */10 * * * * cd /home/client-twenti/Healtify-App && ./make-key.sh >> logs/make-key.log 2>&1

set -euo pipefail

CONTAINER="${HEALTHIFY_CONTAINER:-healtify_backend}"

cd "$(dirname "$0")"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "Container '$CONTAINER' tidak berjalan." >&2
    echo "Jalankan 'docker compose up -d' lebih dulu, atau setel HEALTHIFY_CONTAINER." >&2
    exit 1
fi

exec docker exec "$CONTAINER" python manage.py issue_pending_keys "$@"
