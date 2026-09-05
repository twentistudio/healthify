# Healtify App

## AI Health Claim Verification System

Healtify App adalah aplikasi AI Health Detector berbasis web yang memverifikasi klaim kesehatan menggunakan pendekatan **Retrieval Augmented Generation (RAG)** dengan **Large Language Models (LLMs)**.

Deployment saat ini berjalan **penuh di atas OpenAI** (`gpt-5.4-mini` untuk penalaran & terjemahan, `text-embedding-3-small` untuk embedding). Penyedia lain tetap didukung lewat konfigurasi, tanpa perubahan kode.

### ✨ Fitur Utama

- 🔍 **Bilingual Search**: Pencarian otomatis dalam bahasa Indonesia dan English
- 📚 **Multi-Source Retrieval**: Mengambil paper dari 6 sumber akademik:
  - PubMed (medical journals)
  - CrossRef (scholarly articles)
  - Semantic Scholar (AI/CS papers)
  - ScienceDirect/Elsevier (scientific journals)
  - Google Books (medical textbooks)
  - Open Library (open-access books)
- 🤖 **AI-Powered Verification**: LLM menganalisis klaim **hanya berdasarkan evidence** yang diambil dari knowledge base
- 📊 **Evidence-Based**: Menyediakan sumber jurnal ilmiah sebagai bukti
- 🌐 **REST API**: Backend Django dengan endpoint untuk verifikasi klaim

---

## 🧠 Health Intelligence Engine (kapabilitas tambahan)

Selain produk verifikasi klaim, Healthify mengekspos *health intelligence
engine*-nya untuk consumer eksternal seperti **HealthTalk**, tanpa mengubah
satu pun kontrak API yang sudah ada.

| Endpoint | Kegunaan |
|---|---|
| `POST /api/v1/intelligence/query` | Pertanyaan / keluhan / klaim kesehatan, mendukung percakapan multi-turn |
| `POST /api/v1/intelligence/summary` | Ringkasan konsultasi terstruktur beserta provenance |
| `GET /api/v1/intelligence/sessions/{id}` | Riwayat sesi + health context kumulatif |
| `GET /api/v1/intelligence/capabilities` | Mode, intent, dan fitur yang didukung |

Kemampuan:

- **Query understanding** — CLAIM_VERIFICATION, SYMPTOM_CONTEXT, HEALTH_INFORMATION,
  FOLLOW_UP, MEDICATION_INFORMATION, GENERAL_HEALTH, UNSUPPORTED.
- **Structured health context** — gejala, durasi, keparahan, awitan, obat, alergi,
  riwayat. Field yang tidak dilaporkan pengguna tetap `null` (tidak dikarang).
- **Conversation context** — "Saya demam" + "Sudah tiga hari" → `duration = "3 hari"`.
- **Evidence grounding + provenance** — tiap pernyataan dapat ditelusuri ke sumbernya.
- **Safety layer** — PASS / MODIFY / BLOCK; mendeteksi sinyal gawat darurat,
  bahasa diagnosis pasti, anjuran dosis, dan klaim tak berdasar.
- **Preliminary assessment** — selalu berlabel `PRELIMINARY_ASSESSMENT`, bukan diagnosis.
- **Consultation summary** — hanya dari informasi yang benar-benar muncul di percakapan.

### 🔒 Integritas sumber (perbaikan link DOI 404)

Sebelumnya DOI yang dihasilkan LLM langsung dijadikan `https://doi.org/<doi>`
tanpa pernah dicek, sehingga pengguna bisa menerima link 404. Sekarang:

1. `call_ai_direct` mengambil evidence dari knowledge base **lebih dulu**; daftar
   `sources` berasal dari evidence itu, bukan dari keluaran LLM.
2. Setiap DOI dinormalisasi, dicek formatnya, lalu diverifikasi ke **DOI Handle
   System** (fallback Crossref). DOI yang tidak terdaftar **dibuang**.
3. URL non-DOI dicek via HEAD; 404/410/5xx dibuang.
4. Jika status tidak dapat dipastikan, `url` dikosongkan — lebih baik tanpa link
   daripada link mati.
5. URL/DOI yang ditulis LLM di badan jawaban tapi tidak ada di evidence dihapus.
6. Tanpa bukti memadai → `INSUFFICIENT_EVIDENCE`, LLM tidak diminta menebak.

### 🛠️ Perkakas operasional

```bash
# Isi knowledge base dengan artikel jurnal NYATA dari Crossref.
# Setiap DOI diverifikasi ke registry sebelum disimpan.
python manage.py import_journals --topics-id --rows 20
python manage.py import_journals --query "demam berdarah" --query "hipertensi"
python manage.py import_journals --query "asma" --dry-run    # lihat dulu

# Audit tautan yang SUDAH ada di database (warisan DOI karangan sebelum perbaikan).
python manage.py audit_source_links                      # laporan saja
python manage.py audit_source_links --fix                # bersihkan yang rusak
python manage.py audit_source_links --fix --delete-orphans
```

> Knowledge base yang kosong membuat sistem selalu menjawab
> `INSUFFICIENT_EVIDENCE` — perilaku yang benar, tapi tidak berguna.
> `import_journals` adalah prasyarat operasional setelah deploy.

### 🌐 Pencarian bilingual

Pertanyaan Bahasa Indonesia dicocokkan ke jurnal berbahasa Inggris lewat
pemetaan istilah deterministik: `demam berdarah` → `dengue fever`,
`tifus` → `typhoid`, `hipertensi` → `hypertension`, dan seterusnya. Konsep
kesehatan diberi bobot lebih tinggi daripada kata pengisi kalimat, sehingga
"Saya demam tinggi tiga hari" tidak tenggelam oleh token "tinggi"/"tiga"/"hari".

### 🔁 Ketahanan provider LLM

Deployment ini disetel `LLM_PROVIDER=openai`, yang bersifat
**eksklusif** — hanya OpenAI yang dipakai, tidak ada percobaan ke penyedia lain.

Bila nilai itu dikosongkan, seluruh penyedia yang punya kredensial dipakai
sebagai rantai fallback (`gemini` → `openai` → OpenAI-compatible): provider yang
gagal dilewati selama 5 menit dan permintaan diteruskan ke berikutnya.

`LLM_PROVIDER` dan `LLM_MODEL` adalah variabel yang **sudah** dipakai
`training/scripts/prompt_and_verify.py`, jadi satu setelan berlaku untuk backend
maupun pipeline training — tidak ada nama variabel baru untuk hal yang sama.

Parameter panggilan menyesuaikan keluarga model secara otomatis: GPT-5 ke atas
menolak `max_tokens` dan memakai `max_completion_tokens`, sehingga model bisa
diganti lewat `LLM_MODEL` saja tanpa menyentuh kode.

Bila tidak ada penyedia yang berhasil, engine tetap menjawab memakai jalur
ekstraktif deterministik langsung dari cuplikan evidence — layanan tidak pernah
mati total karena masalah kredensial.

Tiga jalur yang dulu terikat Gemini kini mengikuti konfigurasi yang sama:

| Jalur | Dulu | Sekarang |
|---|---|---|
| Penalaran & verifikasi klaim | Gemini/OpenAI campur | rantai provider terkonfigurasi |
| Terjemahan `/api/translate/` | Gemini saja — diam-diam mengembalikan teks asli bila kunci tidak ada | rantai provider (OpenAI) |
| Embedding jurnal & query | `embed_texts_gemini` saja | `text-embedding-3-small` dengan `dimensions=768` agar cocok dengan kolom vektor yang sudah ada — **tanpa migrasi tabel atau re-embed korpus lama** |

### 📖 Dokumentasi API (Scalar)

| URL | Isi |
|---|---|
| `/docs` (alias `/api/docs`) | Referensi API interaktif — Scalar |
| `/openapi.json` (alias `/api/openapi.json`) | Spesifikasi OpenAPI 3.1 |

Detail lebih lanjut:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/HEALTHTALK_INTEGRATION.md](docs/HEALTHTALK_INTEGRATION.md)

---

# 🧪 Rencana Pengujian (SQA)

## Blackbox Testing

Tujuan: Memvalidasi fungsionalitas sistem dari sudut pandang pengguna tanpa melihat struktur kode internal. Cakupan mencakup alur utama pengguna publik dan admin.

| ID Test Case | Nama Fitur | Deskripsi Skenario Pengujian | Data Uji (Input) | Langkah Pengujian | Hasil yang Diharapkan | Hasil Aktual | Status (Pass / Fail) |
|---|---|---|---|---|---|---|---|
| BB-01 | Verifikasi Klaim (Publik) | Pengguna memasukkan klaim kesehatan dan melakukan verifikasi | “Merokok menyebabkan kanker paru-paru” | Buka halaman Home, isi kolom klaim, klik “Verifikasi Sekarang” | Sistem menampilkan label (Valid/Hoax/Tidak Pasti/Tidak Terverifikasi), confidence, ringkasan, dan referensi | Belum diuji | Belum diuji |
| BB-02 | Verifikasi Klaim – Input Kosong | Pengguna menekan verifikasi tanpa mengisi klaim | “” (kosong) | Buka Home, kosongkan input, klik “Verifikasi Sekarang” | Muncul notifikasi kesalahan input wajib | Belum diuji | Belum diuji |
| BB-03 | Terjemah Hasil – ke Bahasa Indonesia | Pengguna mengubah bahasa UI ke “id” sehingga ringkasan hasil diterjemahkan | Hasil verifikasi yang memiliki summary | Setelah hasil tampil, ubah bahasa ke “id” | Ringkasan dan klaim ditampilkan dalam bahasa Indonesia | Belum diuji | Belum diuji |
| BB-04 | Terjemah Hasil – ke Bahasa Inggris | Pengguna mengubah bahasa UI ke “en” | Hasil verifikasi yang memiliki summary | Setelah hasil tampil, ubah bahasa ke “en” | Ringkasan dan klaim ditampilkan dalam bahasa Inggris | Belum diuji | Belum diuji |
| BB-05 | Detail Klaim | Pengguna membuka detail klaim dari daftar atau tautan | ID klaim valid | Akses /api/claims/{id} atau halaman detail admin | Detail klaim beserta hasil verifikasi ditampilkan (200 OK) | Belum diuji | Belum diuji |
| BB-06 | Riwayat Klaim – Pencarian & Filter | Pengguna melakukan pencarian dan filter label | search=”merokok”, label=”valid” | Buka Admin Claims, isi filter, klik cari | Daftar klaim terfilter sesuai parameter | Belum diuji | Belum diuji |
| BB-07 | Cek Duplikat Klaim | Pengguna memeriksa apakah klaim serupa sudah ada | text=”Minum kopi mencegah penyakit jantung” | Kirim POST ke /api/claims/check-duplicate/ | Sistem mengembalikan indikator is_duplicate, tingkat kecocokan, dan id klaim terkait | Belum diuji | Belum diuji |
| BB-08 | Buat Dispute – DOI | Pengguna melaporkan hasil verifikasi dengan bukti DOI | claim_text, reason (≥20), supporting_doi | Buka halaman Report, isi form, submit | Dispute berhasil dibuat, notifikasi berhasil | Belum diuji | Belum diuji |
| BB-09 | Buat Dispute – Upload PDF tidak valid | Pengguna mengunggah file non-PDF | File .png / .docx | Buka Report, pilih upload PDF, unggah file non-PDF | Muncul validasi format tidak sesuai | Belum diuji | Belum diuji |
| BB-10 | Admin Login – Berhasil | Admin login dengan kredensial benar | username, password valid | Buka /admin/login, isi kredensial benar, submit | Mendapatkan token & diarahkan ke dashboard | Belum diuji | Belum diuji |
| BB-11 | Admin Login – Gagal | Admin login dengan kredensial salah | username/password salah | Buka /admin/login, isi kredensial salah, submit | Tampil pesan “Username atau password salah” (401) | Belum diuji | Belum diuhi |
| BB-12 | Dashboard Admin – Statistik | Admin mengakses statistik | Token valid | Buka /admin/dashboard | Statistik klaim, disputes, sources, aktivitas terbaru ditampilkan | Belum diuji | Belum diuji |
| BB-13 | Admin – Daftar Klaim | Admin melihat daftar klaim | Token valid | Buka /admin/claims | Daftar klaim tampil dengan pagination dan filter | Belum diuji | Belum diuji |
| BB-14 | Admin – Approve Dispute (Re-Verify) | Admin menyetujui dispute dengan re-verify AI | Dispute pending terkait klaim | Buka detail dispute, pilih approve + re-verify, submit | VerificationResult diperbarui sesuai hasil AI, catatan review tercatat | Belum diuji | Belum diuji |
| BB-15 | Admin Logout | Admin melakukan logout | Refresh token | Submit /api/admin/logout dengan refresh token | Logout berhasil, token diblacklist | Belum diuji | Belum diuji |

Catatan: “Belum diuji” menandakan rencana uji; status Pass/Fail diisi setelah eksekusi pengujian manual/otomatis.

Referensi fitur terkait:
- UI Publik: Hero.jsx, Home.jsx
- API Publik: urls.py, views.py
- Pelaporan Dispute: Report.jsx, serializers.py (DisputeCreateSerializer), views.py (DisputeCreateView)
- Admin & Autentikasi: auth_views.py (AdminLoginView), halaman Admin

---

## Whitebox Testing

Tujuan: Menguji berdasarkan struktur logika dan alur kode program (internal), meliputi control flow, decision, loop, dan kondisi logika. Pendekatan cakupan: Statement Coverage, Decision Coverage, dan Path Coverage (jika relevan).

| ID Test Case | Modul / Fungsi | Deskripsi Logika yang Diuji | Jalur Eksekusi (Path) | Kondisi / Keputusan yang Diuji | Data Uji | Hasil yang Diharapkan | Status (Pass / Fail) |
|---|---|---|---|---|---|---|---|
| WB-01 | ClaimVerifyView.post (views.py) | Validasi payload, cabang cache hit/miss, penanganan exception | Invalid serializer → 400; Cache hit → return 200; Cache miss → create+process; Exception → handler | serializer.is_valid(); is_cached; try/except | {text: “...”} | Respons sesuai cabang (400/200) dan verifikasi dibuat saat miss | Belum diuji |
| WB-02 | ClaimVerifyView._process_verification (views.py) | Pengolahan hasil AI: mapping label, pembuatan VerificationResult, loop sumber | AI returns label invalid → fallback “unverified”; loop sumber create ClaimSource | label ∈ {valid,hoax,uncertain,unverified}; sources_data iterasi | Payload AI berbagai label/sumber | VerificationResult sesuai mapping; sumber ditautkan unik | Belum diuji |
| WB-03 | translate_verification_result (views.py) | Validasi input, cache translate summary/claim, mapping label | Missing fields → 400; Dengan summary/claim → cache set/get | label/summary/claim_text presence; target_language | Variasi kombinasi field | Terjemahan atau error 400 bila semua kosong | Belum diuji |
| WB-04 | check_claim_duplicate (views.py) | Validasi input, pemilahan duplikat via matcher, penjelasan skor | text kosong → 400; hasil matcher (exact/high/medium/low) | result.match_found; thresholds | text: berbagai kemiripan | JSON berisi is_duplicate, similarity, explanation | Belum diuji |
| WB-05 | ClaimSimilarityMatcher.find_duplicates (text_normalization.py) | Loop atas klaim, klasifikasi ke level, pemilihan best match | exact → return; else fuzzy/semantic; no match → default | similarity >= high/medium/low | Daftar klaim kandidat | Dict hasil dengan level & best_match | Belum diuji |
| WB-06 | normalize_ai_response (ai_adapter.py) | Normalisasi confidence [0..1], mapping label, aturan “hoax” tidak dibalik, syarat jurnal | confidence out-of-range; raw_label “hoax”; has_journal False | map_ai_label_to_backend; determine_verification_label | AI payload variasi | Final label mengikuti aturan, confidence None untuk unverified | Belum diuji |
| WB-07 | AdminLoginView.post (auth_views.py) | Validasi input, autentikasi, pemeriksaan is_staff, token JWT | Missing creds → 400; auth fail → 401; non-staff → 403; success → 200 | authenticate(); user.is_staff | Kombinasi valid/salah | Respons sesuai cabang dan token saat sukses | Belum diuji |
| WB-08 | AdminDashboardStatsView.get (admin_views.py) | Agregasi counts, loop recent activity, handling atribut | recent_claims tanpa verifikasi; dengan verifikasi | hasattr(claim, 'verification_result') | DB beragam | JSON statistik dan daftar aktivitas terbaru | Belum diuji |
| WB-09 | AdminDisputeDetailView._handle_approve (admin_views.py) | Jalur manual_update vs re_verify, update VerificationResult, fallback saat AI error | manual_update True → update langsung; re_verify True → panggil AI; exception → fallback manual | manual_update, re_verify | Dispute pending + VR ada | Status dispute APPROVED, VR ter-update sesuai jalur | Belum diuji |
| WB-10 | ClaimListView._parse_query_params (views.py) | Validasi parameter page, per_page, label | page<1 atau tidak integer → ValueError; per_page>MAX → clamp | label ∈ VALID_LABELS | Variasi parameter | Raise 400 bila invalid; clamp per_page | Belum diuji |
| WB-11 | translate_text_gemini (views.py) | Fallback jika client None, parsing hasil model candidate/parts | client None → return original; candidates ada → extract | hasattr(resp.text/candidates) | Teks panjang/pendek | Terjemahan atau original text bila gagal | Belum diuji |
| WB-12 | ClaimDetailView.get (views.py) | Ambil klaim via select_related, handling Http404 & 500 | ID valid → 200; ID invalid → Http404; exception → 500 | get_object_or_404 | ID ada/tidak ada | Respons sesuai kondisi | Belum diuji |

Cakupan:
- Statement Coverage: WB-01, WB-02, WB-03, WB-06, WB-07, WB-10, WB-11, WB-12
- Decision Coverage: WB-01, WB-04, WB-06, WB-07, WB-09, WB-10
- Path Coverage: WB-01 (cache hit/miss/exception), WB-09 (manual vs re_verify vs fallback)

Referensi tambahan modul:
- Verifikasi AI: ai_adapter.py (normalize_ai_response)
- Normalisasi & kemiripan: text_normalization.py
- Dispute & email: admin_views.py, email_service.py

Tests:
- ✅ Language detection (Indonesian/English)
- ✅ Query translation
- ✅ Bilingual query generation
- ✅ Parser detection for all 6 sources
- ✅ Small API fetch (optional, requires API keys)
