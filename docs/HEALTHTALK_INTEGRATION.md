# Integrasi HealthTalk ↔ Healthify

HealthTalk memakai Healthify sebagai **health intelligence service** melalui
kontrak HTTP. HealthTalk **tidak pernah** mengakses database Healthify secara
langsung.

### Dokumentasi API

| URL | Isi |
|---|---|
| `https://<host>/docs` | Referensi API interaktif (Scalar), lengkap dengan contoh & "Try it" |
| `https://<host>/openapi.json` | Spesifikasi OpenAPI 3.1 mentah — untuk generate klien |
| `https://<host>/api/docs` | Alias, berguna bila hanya prefix `/api/` yang di-proxy |
| `https://<host>/api/openapi.json` | Alias spesifikasi |

Daftar `servers` di dalam spec diturunkan otomatis dari `ALLOWED_HOSTS`, jadi
tombol "Try it" di Scalar menembak host yang benar tanpa konfigurasi tambahan.

---

## 1. Autentikasi

### Membuat API key

Key hanyalah string acak — tidak ada pendaftaran, tidak ada tabel database.
Buat dengan generator kriptografis (jangan `uuid4` atau `random`):

```bash
python3 -c "import secrets; print('ht_live_' + secrets.token_urlsafe(32))"
# ht_live_ruoJmUpPOaSPm_ozBLgIyfi3eB8xU8yrsXdQR3-B7-o
```

Pasang di `.env` sisi Healthify dengan format `key:nama_consumer`,
dipisah koma untuk beberapa consumer:

```bash
INTELLIGENCE_API_KEYS="ht_live_xxx:healthtalk,ht_stag_yyy:healthtalk-staging"
```

`nama_consumer` muncul di `metadata.consumer` pada setiap response dan
tersimpan di `ConversationSession.consumer`, jadi percakapan tiap consumer
bisa dibedakan.

- **Rotasi:** tambahkan key baru di samping yang lama, minta HealthTalk
  berganti, lalu hapus key lama. Tidak ada downtime.
- **Cabut akses:** hapus barisnya lalu restart. Key langsung tidak berlaku.

HealthTalk mengirim:

```
X-API-Key: ht_live_xxx
```

> Bila `INTELLIGENCE_API_KEYS` dikosongkan, endpoint `/api/v1/intelligence/*`
> **terbuka tanpa autentikasi** dan identitas consumer diambil dari header
> opsional `X-Consumer`. Itu hanya untuk pengembangan lokal.

---

## 2. Alur percakapan

```
HealthTalk                        Healthify
    │                                 │
    │  POST /api/v1/intelligence/query│   (session_id sama tiap giliran)
    ├────────────────────────────────▶│
    │                                 │  understanding → context → evidence
    │                                 │  → reasoning → safety
    │◀────────────────────────────────┤
    │  answer + health_context +      │
    │  evidence + safety_flags        │
    │                                 │
   ... percakapan berlanjut ...       │
    │                                 │
    │ POST /api/v1/intelligence/summary
    ├────────────────────────────────▶│
    │◀────────────────────────────────┤
    │  ringkasan terstruktur + provenance
```

### Giliran pertama

```bash
curl -X POST https://api.healthify.cloud/api/v1/intelligence/query \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ht_live_xxx' \
  -d '{
        "query": "Saya sudah demam tiga hari dan batuk.",
        "mode": "consultation",
        "context": { "session_id": "HT-001" }
      }'
```

### Giliran berikutnya — cukup kirim `session_id` yang sama

```bash
curl -X POST https://api.healthify.cloud/api/v1/intelligence/query \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ht_live_xxx' \
  -d '{ "query": "Apakah itu normal?", "context": { "session_id": "HT-001" } }'
```

Healthify menyimpan riwayat dan health context kumulatif, sehingga
`"Apakah itu normal?"` dipahami sebagai pertanyaan tentang demam 3 hari yang
sedang dibahas — bukan query independen.

> **Mode stateless.** Bila HealthTalk memilih menyimpan riwayat sendiri, kirim
> `context.previous_messages` (array `{role, content}`) tanpa `session_id`.

### Akhir sesi

```bash
curl -X POST https://api.healthify.cloud/api/v1/intelligence/summary \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ht_live_xxx' \
  -d '{ "session_id": "HT-001", "close_session": true }'
```

---

## 3. Mode

| `mode`         | Untuk                                    |
| -------------- | ---------------------------------------- |
| `consultation` | keluhan/gejala pengguna (default)        |
| `claim`        | verifikasi klaim kesehatan               |
| `information`  | pertanyaan informasi kesehatan           |
| `medication`   | pertanyaan seputar obat                  |

`mode` hanya memberi arah. Intent final ditentukan engine dan dikembalikan di
field `intent`.

---

## 4. Field yang WAJIB ditangani HealthTalk

### `safety.decision`

| Nilai    | Arti                                                | Yang harus dilakukan HealthTalk               |
| -------- | --------------------------------------------------- | --------------------------------------------- |
| `PASS`   | jawaban aman apa adanya                             | tampilkan `answer`                            |
| `MODIFY` | jawaban sudah disunting engine                      | tampilkan `answer` (sudah aman); pertimbangkan menonjolkan flag |
| `BLOCK`  | jawaban asli ditolak dan diganti                    | tampilkan `answer` pengganti; jangan tampilkan apa pun dari giliran itu selain ini |

Flag `EMERGENCY_SIGNAL` (severity `critical`) sebaiknya ditampilkan menonjol —
`answer` sudah diawali peringatan gawat darurat.

### `evidence_status`

| Nilai                   | Arti                                                      |
| ----------------------- | --------------------------------------------------------- |
| `SUFFICIENT`            | bukti memadai                                             |
| `PARTIAL`               | bukti terbatas — `uncertainty` terisi                     |
| `INSUFFICIENT_EVIDENCE` | tidak ada bukti memadai; `evidence` kosong; sistem tidak menebak |

### `evidence[].url`

Boleh `null`. Itu **disengaja**: sistem hanya memberikan link yang sudah
dipastikan hidup. Jangan membangun sendiri `https://doi.org/{doi}` dari field
`doi` bila `url` kosong — cek `doi_verified` dan `link_status` dulu.

### `preliminary_assessment`

Selalu `status: "PRELIMINARY_ASSESSMENT"` dan `is_diagnosis: false`.
Tampilkan `disclaimer` bersama isinya. Jangan menyajikannya sebagai diagnosis.

---

## 5. Provenance

`claims[]` memetakan tiap pernyataan pada `answer` ke evidence pendukungnya:

```json
{
  "claim": "Demam tiga hari disertai batuk umumnya berkaitan dengan infeksi saluran napas",
  "verdict": "supported",
  "confidence": 0.85,
  "supporting_evidence": [
    { "source_id": "journal:12", "title": "...", "doi": "10.1016/...", "url": "https://doi.org/10.1016/...", "via": "citation_marker" }
  ]
}
```

Pada ringkasan, setiap bagian membawa `provenance`:

| Nilai                | Arti                                       |
| -------------------- | ------------------------------------------ |
| `USER_REPORTED`      | dilaporkan langsung oleh pengguna          |
| `AI_INFERRED`        | disimpulkan sistem dari percakapan         |
| `EVIDENCE_SUPPORTED` | didukung evidence yang dibahas             |
| `SYSTEM_GENERATED`   | teks sistem (langkah lanjutan, catatan keamanan) |

---

## 6. Penanganan error

| HTTP | `error`           | Penyebab                                    |
| ---- | ----------------- | ------------------------------------------- |
| 400  | `invalid_request` | `query` kosong / >5000 karakter / `mode` tidak dikenal |
| 401  | `unauthorized`    | `X-API-Key` hilang atau salah               |
| 404  | `not_found`       | `session_id` tidak ditemukan                |
| 500  | `engine_error`    | kegagalan internal                          |

---

## 7. Healthify tetap mandiri

Endpoint di atas adalah **tambahan**. Menghapus HealthTalk dari gambar tidak
memengaruhi apa pun: `/api/verify/`, `/api/claims/`, dispute, dan panel admin
Healthify berjalan tanpa menyentuh modul intelligence maupun tabel percakapan.

---

## 8. Runbook deployment

```bash
# 1. Migrasi (Dockerfile CMD sudah menjalankan ini otomatis)
python manage.py migrate --noinput

# 2. Bersihkan tautan warisan yang rusak — sekali saja setelah upgrade
python manage.py audit_source_links --fix

# 3. Isi knowledge base (WAJIB — tanpa ini semua jawaban INSUFFICIENT_EVIDENCE)
python manage.py import_journals --topics-id --rows 20

# 4. Verifikasi
curl -s https://<host>/api/health/
curl -s https://<host>/api/v1/intelligence/capabilities
curl -s -X POST https://<host>/api/v1/intelligence/query \
     -H 'Content-Type: application/json' -H 'X-API-Key: <key>' \
     -d '{"query":"Saya demam tiga hari","mode":"consultation"}'
```

### Variabel environment

| Variabel | Wajib | Keterangan |
|---|---|---|
| `INTELLIGENCE_API_KEYS` | untuk produksi | `"key:consumer"` dipisah koma. Kosong = endpoint terbuka. |
| `ALLOWED_HOSTS` | ya | Host yang dilayani, dipisah koma. Tidak ada domain bawaan; wildcard subdomain tidak didukung. |
| `FRONTEND_URL` | untuk browser | Origin yang diizinkan CORS, dipisah koma. Origin HealthTalk masuk di sini — tanpa variabel baru. |
| `EVIDENCE_LINK_CHECK_ENABLED` | tidak | Default `True`. Jangan dimatikan di produksi. |
| `LLM_PROVIDER` | tidak | Provider yang dipakai, **eksklusif**. `openai` = hanya OpenAI. Kosong = semua provider berkredensial dengan fallback. Variabel yang sama dibaca pipeline training. |
| `LLM_MODEL` | tidak | Nama model. Default `gpt-5.4-mini`. Parameter batas token menyesuaikan keluarga model otomatis. |
| `EMBEDDINGS_ENABLED` | tidak | `0` untuk mematikan embedding; retrieval tetap jalan leksikal. |
| `EMBEDDING_MODEL` | tidak | Default `text-embedding-3-small`. |
| `EMBEDDING_DIMENSIONS` | tidak | Harus cocok dengan kolom vektor (pipeline training: `768`). Kosong = deteksi otomatis. |
| `INTELLIGENCE_LLM_ENABLED` | tidak | `0` untuk mematikan LLM (jalur ekstraktif). |
| `OPENAI_API_KEY` | ya | Dipakai untuk penalaran, terjemahan, dan embedding. |
| `GEMINI_API_KEY` / `GEMINI_API` | tidak | Hanya relevan bila Gemini masuk `LLM_PROVIDER`. |

### Catatan CORS

Origin harus disebut eksplisit. `CORS_ALLOW_CREDENTIALS` dimatikan secara
default dan Healthify menolak konfigurasi yang mengaktifkannya bersamaan dengan
`CORS_ALLOWED_ORIGIN_REGEXES` — kombinasi itu memungkinkan pemilik domain lain
membaca respons API atas nama pengunjung yang sedang login.

HealthTalk yang memanggil dari server (bukan browser) tidak terpengaruh CORS
sama sekali; cukup `X-API-Key`.

### Yang perlu dipantau

* Log `[LINK] Membuang DOI tidak valid` — bila sering muncul untuk sumber
  knowledge base, jalankan `audit_source_links --fix`.
* Log `[LLM] Provider '<x>' ditandai tidak sehat` — kredensial provider itu
  bermasalah; layanan tetap jalan lewat fallback.
* Rasio `INSUFFICIENT_EVIDENCE` yang tinggi menandakan knowledge base perlu
  diperluas dengan `import_journals`.
