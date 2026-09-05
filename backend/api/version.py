"""
Versi logika verifikasi Healthify.

Ditempatkan di modul tersendiri tanpa dependensi agar dapat dipakai baik oleh
`models.py` (sebagai default field) maupun oleh lapisan mesin, tanpa membuat
lingkaran impor.

Dinaikkan setiap kali retrieval, pemeringkatan, atau penilaian label berubah
berarti. Hasil dengan versi lebih lama tidak disajikan dari cache.

Riwayat:
    v2.0  verifikasi berbasis LLM tanpa pemeriksaan relevansi topikal.
    v3.0  relevansi topikal: aspek pertanyaan, fokus judul, embedding
          bilingual, judul diambil dari registry, sumber tak tertelusur
          dikecualikan.
    v3.1  lantai kemiripan makna: dokumen yang maknanya jauh dari pertanyaan
          dibuang meski kata kunci dan aspeknya cocok penuh; label
          TIDAK TERVERIFIKASI tidak lagi membawa daftar sumber.
    v3.2  basis pengetahuan diperluas (60 topik), pencocokan istilah pendek
          memakai batas kata, nama penyakit dibakukan, dan mesin membaca lebih
          banyak kandidat serta menyajikan lebih banyak referensi.
"""

VERIFICATION_LOGIC_VERSION = "v3.2"
