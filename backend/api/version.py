"""
Versi logika verifikasi.

Dinaikkan setiap kali retrieval, pemeringkatan, atau penilaian label berubah
berarti; hasil dengan versi lebih lama tidak disajikan dari cache. Berada di
modul tersendiri agar `models.py` dan engine dapat memakainya tanpa lingkaran
impor.

Riwayat:
    v2.0  verifikasi LLM tanpa pemeriksaan relevansi topikal.
    v3.0  relevansi topikal: aspek, fokus judul, embedding bilingual, judul
          dari registry, sumber tak tertelusur dikecualikan.
    v3.1  lantai kemiripan makna; label tidak terverifikasi tidak lagi
          membawa daftar sumber.
    v3.2  basis pengetahuan diperluas, pencocokan istilah pendek memakai batas
          kata, nama penyakit dibakukan, referensi diperbanyak.
"""

VERIFICATION_LOGIC_VERSION = "v3.2"
