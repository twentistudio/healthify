# Healthify — Arsitektur & Compatibility Matrix

> **Prinsip:** *Extend the engine, preserve the product.*
> Healthify tetap produk mandiri. HealthTalk hanya **consumer eksternal** dari
> kapabilitas intelligence-nya.

---

## 1. Peta arsitektur

```
                                HEALTHIFY
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
  Product Layer            Health Intelligence            API Layer
  (tidak berubah)               Engine                 (baru + lama)
        │                           │                           │
  /api/verify/           api/intelligence/            api/urls.py  (lama)
  /api/claims/            ├── query_understanding/    api/intelligence_urls.py (baru)
  /api/translate/         ├── context/                api/docs_urls.py (baru)
  /api/disputes/          ├── retrieval/
  /api/admin/*            ├── reranking/
                          ├── evidence/
                          ├── claims/
                          ├── reasoning/
                          ├── safety/
                          ├── summarization/
                          └── adapters/
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                    Healthify Product     HealthTalk
                                          (consumer eksternal)
```

## 2. Alur engine

```
Health Input
     │
     ▼
Query Understanding ── intent: CLAIM_VERIFICATION | SYMPTOM_CONTEXT |
     │                         HEALTH_INFORMATION | FOLLOW_UP |
     │                         MEDICATION_INFORMATION | GENERAL_HEALTH | UNSUPPORTED
     ▼
Health Context ──────── structured, akumulatif lintas giliran
     │
     ▼
Evidence Retrieval ──── JournalArticle + Source/ClaimSource + indeks pgvector
     │
     ▼
Evidence Validation ─── validasi DOI/URL, buang sumber tak terverifikasi,
     │                  skor kualitas, ranking, klasifikasi kecukupan
     ▼
Reasoning ───────────── Claim Engine  ATAU  Response Engine
     │                  (LLM hanya menyusun kalimat dari evidence)
     ▼
Safety Layer ────────── PASS | MODIFY | BLOCK
     │
     ▼
Response ────────────── Adapter: format Healthify lama  ATAU  format HealthTalk
```

## 3. Compatibility matrix

| Komponen existing            | Status         | Tindakan                                                        |
| ---------------------------- | -------------- | --------------------------------------------------------------- |
| `POST /api/verify/`          | Existing       | **Preserve** — request & response identik                        |
| `ClaimDetailSerializer`      | Existing       | **Preserve** — tidak ada field dihapus/diubah tipe               |
| `Claim`/`VerificationResult`/`Source`/`ClaimSource` | Existing | **Preserve** — tidak ada migrasi destruktif |
| `JournalArticle` (knowledge base) | Existing  | **Reuse** — kini juga jadi sumber grounding                      |
| Tabel embeddings (pgvector)  | Existing       | **Reuse** — dipakai bila modul training tersedia                 |
| `training/scripts/*` (RAG)   | Existing       | **Preserve** — dipanggil lebih dulu, tidak diubah                |
| `ai_adapter.extract_sources` | Existing       | **Extend** — ditambah validasi link (anti-404)                   |
| `ai_adapter.call_ai_direct`  | Existing       | **Refactor** — kini grounded ke knowledge base, tidak lagi minta LLM mengarang sumber |
| `ClaimVerifyView`            | Existing       | **Fix** — `_handle_verification_error` yang hilang ditambahkan   |
| Dispute & admin endpoints    | Existing       | **Preserve**                                                     |
| `POST /api/v1/intelligence/query`   | Baru    | **Add**                                                          |
| `POST /api/v1/intelligence/summary` | Baru    | **Add**                                                          |
| `GET /api/v1/intelligence/sessions/{id}` | Baru | **Add**                                                        |
| `ConversationSession/Message/Summary` | Baru   | **Add** — migrasi 0016, murni `CreateModel`                      |
| Safety layer                 | Baru           | **Add**                                                          |
| Dokumentasi Scalar `/docs`   | Baru           | **Add**                                                          |

## 4. Yang harus tetap immutable

* Bentuk request `POST /api/verify/` → `{"text": "..."}`.
* Bentuk response `ClaimDetailSerializer` (id, text, text_normalized, status,
  created_at, updated_at, verification_result, sources).
* Nilai label: `valid` / `hoax` / `uncertain` / `unverified`, dengan
  `confidence = null` untuk `unverified`.
* Skema tabel `api_claim`, `api_source`, `api_claimsource`,
  `api_verificationresult`, `api_dispute`, `api_journalarticle`.
* Perilaku cache klaim berbasis `text_normalized`.

## 5. Integritas sumber (anti-404)

Modul: [`api/intelligence/evidence/link_validator.py`](../backend/api/intelligence/evidence/link_validator.py)

```
DOI/URL kandidat
      │
      ▼
Normalisasi (buang prefix doi:, https://doi.org/, urn:doi:)
      │
      ▼
Cek format 10.<4-9 digit>/<suffix>  ──── gagal ──▶ DIBUANG (malformed)
      │
      ▼
DOI Handle System (fallback Crossref) ── tidak terdaftar ──▶ DIBUANG (unresolvable)
      │
      ├── terdaftar ────────▶ url = https://doi.org/<doi>, doi_verified = true
      │
      └── tidak dapat dipastikan (jaringan)
                │
                ├── sumber knowledge base internal ──▶ link tetap diberikan
                └── sumber lain ─────────────────────▶ url dikosongkan
```

Lapisan pertahanan:

1. **LLM tidak pernah menjadi sumber sumber.** `call_ai_direct` mengambil
   evidence dari knowledge base terlebih dahulu; daftar `sources` di response
   berasal dari evidence itu, bukan dari keluaran LLM.
2. **Validasi registry.** Setiap DOI dicek ke DOI Handle System / Crossref
   (hasil di-cache 30 hari untuk yang valid, 1 hari untuk yang gagal).
3. **Pembersihan teks jawaban.** URL/DOI yang ditulis LLM di badan jawaban tapi
   tidak ada di daftar evidence dibuang (`strip_fabricated_references`).
4. **Gate publikasi.** `EvidenceItem.is_publishable()` menolak origin
   `MODEL_SUGGESTED` yang DOI-nya belum terverifikasi.
5. **Tanpa bukti = tanpa kesimpulan.** `INSUFFICIENT_EVIDENCE` → LLM tidak
   dipanggil, daftar sumber kosong.
