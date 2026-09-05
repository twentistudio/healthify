import os
import sys
import json
import subprocess
import hashlib
import time
import logging
import requests
import re
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# Path Configuration - Handle both local dev and Docker environments
BACKEND_DIR = Path(__file__).resolve().parent.parent  # /app/ in Docker

# Try multiple possible training locations
_possible_training_dirs = [
    BACKEND_DIR / "training",           # Docker: /app/training/
    BACKEND_DIR.parent / "training",    # Local dev: ../training/
    Path("/app/training"),              # Docker fallback
]

TRAINING_DIR = next((d for d in _possible_training_dirs if d.exists()), BACKEND_DIR / "training")
TRAINING_SCRIPTS_DIR = TRAINING_DIR / "scripts"

VERIFY_SCRIPT = TRAINING_SCRIPTS_DIR / "prompt_and_verify.py"

if not VERIFY_SCRIPT.exists():
    logger.warning(f"Verification script not found at {VERIFY_SCRIPT}")
    logger.warning("Will use direct AI call method")


from .version import VERIFICATION_LOGIC_VERSION  # noqa: F401

# Configuration
VERIFICATION_TIMEOUT = 90  
MAX_RETRIES = 2
SIMPLE_CLAIM_WORD_THRESHOLD = 20

# Global module cache for direct import
_optimized_module = None
_original_module = None

def safe_float(value, default: float = 0.0) -> float:
    """Konversi ke float dengan aman; fallback ke default jika gagal."""
    try:
        if value is None:
            return float(default)
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        return float(s)
    except Exception:
        try:
            return float(default)
        except Exception:
            return 0.0

def validate_url(url: str, timeout: float = 3.0) -> str:
    """Cek cepat apakah URL sumber tampak valid. Jika 404/5xx, kembalikan string kosong."""
    if not url:
        return ""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        status = resp.status_code

        if status in (404, 410) or status >= 500:
            logger.info(f"Dropping unreachable source URL {url} (status={status})")
            return ""

        return resp.url or url
    except Exception as e:
        logger.debug(f"validate_url HEAD failed for {url}: {e}")
        return url

# Helper Functions

def normalize_claim_text(text: str) -> str:
    """Normalisasi teks klaim untuk konsistensi."""
    if not text:
        return ""
    return text.strip().lower()

def is_health_related_claim(claim_text: str, summary: str = "") -> bool:
    """
    IMPROVED: Deteksi health-related dengan support BILINGUAL.
    """
    # Expanded keywords - lebih comprehensive
    health_keywords_id = {
        'kesehatan', 'penyakit', 'obat', 'vitamin', 'diet', 'nutrisi',
        'medis', 'dokter', 'rumah sakit', 'terapi', 'pengobatan',
        'kanker', 'diabetes', 'jantung', 'darah', 'kulit', 'wajah',
        'imun', 'infeksi', 'virus', 'bakteri', 'gejala', 'diagnosa',
        'vaksin', 'antibiotik', 'herbal', 'suplemen', 'olahraga',
        'tidur', 'stress', 'mental', 'depresi', 'kecemasan',
        'merokok', 'rokok', 'tembakau', 'paru', 'asap'  # TAMBAHAN
    }
    
    health_keywords_en = {
        'health', 'disease', 'medicine', 'vitamin', 'diet', 'nutrition',
        'medical', 'doctor', 'hospital', 'therapy', 'treatment',
        'cancer', 'diabetes', 'heart', 'blood', 'skin', 'immune',
        'infection', 'virus', 'bacteria', 'symptom', 'diagnosis',
        'vaccine', 'antibiotic', 'supplement', 'exercise',
        'sleep', 'stress', 'mental', 'depression', 'anxiety',
        'smoking', 'cigarette', 'tobacco', 'lung', 'smoke'  # TAMBAHAN
    }
    
    # Medical patterns untuk deteksi lebih luas
    medical_patterns = [
        r'\b(cause[s]?|menyebabkan)\s+(cancer|kanker|disease|penyakit)',
        r'\b(prevent[s]?|mencegah)\s+(disease|penyakit|infection|infeksi)',
        r'\b(risk|risiko)\s+(of|dari)\s+(cancer|kanker|disease|penyakit)',
        r'\b(smoking|merokok)\b.*\b(lung|paru|cancer|kanker)',
        r'\b(treatment|pengobatan|terapi)\s+(for|untuk)',
    ]
    
    combined_text = (claim_text + " " + summary).lower()
    all_keywords = health_keywords_id | health_keywords_en
    
    # Method 1: Keyword matching
    keyword_matches = sum(1 for kw in all_keywords if kw in combined_text)
    
    # Method 2: Pattern matching
    pattern_matches = sum(1 for pattern in medical_patterns 
                         if re.search(pattern, combined_text, re.I))
    
    total_matches = keyword_matches + pattern_matches
    
    # LOWER threshold - lebih permissive
    is_health = total_matches >= 1  # Changed from 2 to 1
    
    logger.debug("[HEALTH_CHECK] kata kunci=%s pola=%s -> kesehatan=%s",
                 keyword_matches, pattern_matches, is_health)
    
    return is_health

def determine_verification_label(confidence_score: float, has_sources: bool = True, 
                                has_journal: bool = False, claim_text: str = "", 
                                summary: str = "") -> str:
    """Penentuan label akhir berbasis confidence + keberadaan jurnal.

    Aturan global:
    - Jika BUKAN klaim kesehatan ATAU tidak ada jurnal terkait -> UNVERIFIED
    - Jika klaim kesehatan DENGAN jurnal terkait:
        * confidence <= 0.50  -> HOAX
        * 0.50 < confidence < 0.75 -> UNCERTAIN
        * confidence >= 0.75 -> VALID
    """
    try:
        c = float(confidence_score)
    except (TypeError, ValueError):
        c = 0.0

    # Check if health-related
    is_health = is_health_related_claim(claim_text, summary)

    logger.debug(
        "[LABEL] confidence=%.2f sumber=%s jurnal=%s kesehatan=%s",
        c, has_sources, has_journal, is_health,
    )

    # RULE A: Jika BUKAN klaim kesehatan ATAU tidak ada jurnal terkait -> UNVERIFIED
    # Di sini kita mensyaratkan keberadaan jurnal (DOI / source_type='journal'),
    # bukan hanya website biasa.
    if (not is_health) or (not has_journal):
        logger.info("[LABEL] -> UNVERIFIED (non-health topic or no journal sources)")
        return "unverified"

    # RULE B: Klaim kesehatan dengan jurnal terkait
    #  - c >= 0.75  -> VALID
    #  - c <= 0.50  -> HOAX
    #  - 0.50 < c < 0.75 -> UNCERTAIN
    if c >= 0.75:
        logger.info(f"[LABEL] -> VALID (confidence {c:.2f} >= 0.75)")
        return "valid"
    if c <= 0.50:
        logger.info(f"[LABEL] -> HOAX (confidence {c:.2f} <= 0.50)")
        return "hoax"

    logger.info(f"[LABEL] -> UNCERTAIN (0.50 < {c:.2f} < 0.75)")
    return "uncertain"

def map_ai_label_to_backend(ai_label: str) -> str:
    """Map label dari AI ke format backend."""
    if not ai_label:
        return 'unverified'
    
    label_lower = ai_label.lower().strip()
    
    label_mapping = {
        'true': 'valid', 'valid': 'valid', 'supported': 'valid', 
        'verified': 'valid', 'benar': 'valid', 'fakta': 'valid',
        
        'false': 'hoax', 'hoax': 'hoax', 'refuted': 'hoax',
        'debunked': 'hoax', 'salah': 'hoax',
        
        'uncertain': 'uncertain', 'partially_valid': 'uncertain',
        'partial': 'uncertain', 'misleading': 'uncertain',
        'mixed': 'uncertain', 'tidak_pasti': 'uncertain',
        
        'unverified': 'unverified', 'inconclusive': 'unverified',
        'unclear': 'unverified', 'insufficient': 'unverified',
    }
    
    return label_mapping.get(label_lower, 'unverified')

def normalize_ai_response(ai_result: Dict[str, Any], claim_text: str = "") -> Dict[str, Any]:
    """
    FIXED: Normalisasi response dengan logging detail.
    """
    raw_label = ai_result.get('label', 'unverified')
    confidence_raw = ai_result.get('confidence', 0)
    
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    
    if confidence > 1.0 and confidence <= 100.0:
        confidence /= 100.0
    confidence = max(0.0, min(confidence, 1.0))
    
    # Map raw label dari AI ke skema backend (valid/hoax/uncertain/unverified)
    mapped_label = map_ai_label_to_backend(raw_label)

    # Extract sources
    sources = extract_sources(ai_result)
    
    # Build summary
    original_summary = (ai_result.get('summary') or "").strip()
    combined_summary = original_summary or "Tidak ada ringkasan tersedia."
    
    # Detect journal presence
    has_journal = any(
        (s.get('doi') or '').strip() or s.get('source_type') == 'journal'
        for s in sources
    )
    
    logger.info(f"[NORMALIZE] Raw label: {raw_label} (mapped: {mapped_label}), Confidence: {confidence:.2f}")
    logger.info(f"[NORMALIZE] Has journal: {has_journal}, Total sources: {len(sources)}")
    
    # Penilaian AI tidak boleh "dinaikkan" oleh tangga confidence.
    #
    # `confidence` dari model adalah keyakinan terhadap penilaiannya sendiri,
    # bukan probabilitas bahwa klaim itu benar. Sebelumnya model yang menjawab
    # "uncertain" dengan confidence tinggi (mis. yakin bahwa bukti tidak
    # membahas klaim) tetap dipromosikan menjadi VALID oleh tangga confidence —
    # sistem bisa melabeli FAKTA untuk klaim yang justru tidak didukung bukti.
    #
    # Aturan sekarang:
    #   hoax                  -> tetap HOAX (tidak pernah dibalik)
    #   uncertain/unverified  -> tidak pernah dipromosikan ke VALID
    #   valid                 -> tangga confidence tetap berlaku, dan hanya
    #                            dapat MENURUNKAN label (butuh jurnal + skor)
    if mapped_label == 'hoax':
        final_label = 'hoax'
        final_confidence = confidence
        logger.info("[NORMALIZE] Final label tetap HOAX sesuai penilaian AI")
    elif mapped_label in ('uncertain', 'unverified'):
        # Tetap wajibkan keberadaan jurnal seperti aturan lama.
        if not has_journal or not is_health_related_claim(claim_text, combined_summary):
            final_label = 'unverified'
            final_confidence = None
        else:
            final_label = mapped_label
            final_confidence = confidence if final_label != 'unverified' else None
        logger.info(
            "[NORMALIZE] Penilaian AI '%s' dipertahankan (tidak dipromosikan ke VALID)",
            mapped_label,
        )
    else:
        # Determine final label dengan improved logic (termasuk heuristic merokok-kanker)
        final_label = determine_verification_label(
            confidence_score=confidence,
            has_sources=bool(sources),
            has_journal=has_journal,
            claim_text=claim_text,
            summary=combined_summary
        )

        # IMPORTANT: Jika label unverified, set confidence ke None
        final_confidence = confidence if final_label != 'unverified' else None
    
    # Penanda sitasi dibuang dari teks yang akan dibaca pengguna. Ia notasi
    # internal, dan di layar hanya tampak sebagai angka dalam kurung yang tidak
    # berarti apa-apa; daftar referensi disajikan terpisah di bawah jawaban.
    from .intelligence.citations import strip_citation_markers

    combined_summary = strip_citation_markers(combined_summary)

    # Label UNVERIFIED berarti sistem tidak dapat menyimpulkan apa pun. Tetap
    # melampirkan daftar sumber membantah label itu sendiri: pembaca melihat
    # referensi seolah klaimnya tertelusur, padahal justru sebaliknya.
    if final_label == 'unverified' and sources:
        logger.info("[NORMALIZE] %d sumber dilepas karena label UNVERIFIED", len(sources))
        sources = []

    logger.info(f"[NORMALIZE] Final: label={final_label}, confidence={final_confidence}")
    
    return {
        'label': final_label,
        'confidence': final_confidence,
        'summary': combined_summary,
        'sources': sources,
        '_original_label': raw_label,
        '_debug': {
            'claim_text': claim_text[:100],
            'has_journal': has_journal,
            'source_count': len(sources)
        }
    }


def extract_sources(result: Dict[str, Any], trusted: bool = False) -> List[Dict[str, Any]]:
    """
    Ekstrak sources dari result dictionary dengan normalisasi + VALIDASI LINK.

    PENTING (perbaikan anti-404):
        Sebelumnya DOI yang datang dari LLM langsung diubah menjadi
        `https://doi.org/<doi>` tanpa pernah dicek. Akibatnya user bisa
        menerima link DOI yang tidak ada / 404.

        Sekarang setiap DOI dan URL divalidasi lewat
        `api.intelligence.evidence.link_validator`:
          - format DOI ngawur   -> sumber dibuang
          - DOI tidak terdaftar -> sumber dibuang
          - tidak bisa dicek    -> DOI disimpan sebagai metadata, tetapi
                                   URL TIDAK diberikan (kecuali `trusted=True`
                                   untuk sumber dari knowledge base sendiri)

    Args:
        trusted: True bila sumber berasal dari knowledge base Healthify
            (kurasi admin / indeks vektor), bukan karangan LLM.
    """
    from .intelligence.evidence import link_validator as lv

    sources = []

    sources_raw = (
        result.get("sources") or 
        result.get("neighbors") or 
        result.get("evidence") or 
        result.get("references") or
        []
    )
    
    if not isinstance(sources_raw, list):
        logger.warning(f"sources is not a list: {type(sources_raw)}")
        return []
    
    dropped = 0
    for src in sources_raw:
        if not isinstance(src, dict):
            continue
        
        raw_doi = (src.get("doi") or "").strip()
        raw_url = (src.get("url") or "").strip()
        safe_id = (src.get("safe_id") or "").strip()
        is_trusted = bool(trusted or src.get("_from_dispute") or src.get("_trusted"))

        validated = lv.validate_reference(raw_doi, raw_url, trust_on_unknown=is_trusted)
        doi = validated["doi"]
        url = validated["url"]

        # Sumber yang mengklaim DOI tapi DOI-nya tidak sah/ tidak ada -> buang.
        if raw_doi and not doi:
            dropped += 1
            logger.warning(
                "[SOURCES] Sumber dibuang karena DOI tidak dapat diverifikasi (%s): %r",
                validated["link_status"], raw_doi[:120],
            )
            continue

        # Sumber non-DOI yang link-nya dipastikan mati -> buang.
        if raw_url and not url and validated["link_status"] in (
            lv.STATUS_UNRESOLVABLE, lv.STATUS_MALFORMED
        ):
            dropped += 1
            logger.info("[SOURCES] Sumber dibuang karena URL tidak dapat dijangkau: %s", raw_url[:120])
            continue

        # Minimal identifier supaya bisa dilacak di frontend / database
        identifier = doi or url or safe_id
        if not identifier:
            dropped += 1
            continue
        
        raw_title = src.get("title") or safe_id or "Unknown"
        snippet = (src.get("snippet") or src.get("text") or "").strip()
        if raw_title == "Unknown" and snippet:
            raw_title = snippet[:80] + ("..." if len(snippet) > 80 else "")
        
        excerpt = snippet[:500]
        
        source_obj = {
            "title": raw_title,
            "doi": doi,
            "url": url,
            "relevance_score": safe_float(
                src.get("relevance_score", src.get("relevance", 0.0)),
                default=0.0,
            ),
            "excerpt": excerpt,
            "source_type": src.get("source_type", "journal"),
            "_doi_verified": validated["doi_verified"],
            "_link_status": validated["link_status"],
        }
        
        sources.append(source_obj)

    if dropped:
        logger.info("[SOURCES] %d sumber dibuang karena gagal validasi link", dropped)

    # Urutkan dari yang paling relevan dan ambil maksimal 5 untuk ditampilkan di frontend
    sources.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return sources[:5]

def load_training_env() -> Dict[str, str]:
    """
    Load environment variables dari training/.env dengan validation.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(TRAINING_SCRIPTS_DIR)
    
    dotenv_path = TRAINING_DIR / ".env"
    
    if dotenv_path.exists():
        try:
            from dotenv import dotenv_values
            env_vars = dotenv_values(dotenv_path)
            
            critical_keys = ["DEEPSEEK_API_KEY", "GEMINI_API_KEY", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER"]
            missing_keys = [k for k in critical_keys if not env_vars.get(k)]
            
            if missing_keys:
                logger.warning(f"⚠️  Missing keys in training/.env: {missing_keys}")
            else:
                logger.debug("✅ All critical env keys present")
            
            env.update({k: v for k, v in env_vars.items() if v is not None})
            
            logger.info(f"✅ Loaded .env from: {dotenv_path}")
            logger.debug(f"   Keys loaded: {list(env_vars.keys())}")
            
        except ImportError:
            logger.error("❌ python-dotenv not installed! Cannot load .env file")
        except Exception as e:
            logger.error(f"❌ Error loading .env: {e}")
    else:
        logger.warning(f"⚠️  .env not found at: {dotenv_path}")
        logger.info("   Using environment variables from system")
    
    return env

def parse_json_from_output(output: str) -> Optional[Dict[str, Any]]:
    """
    Parse JSON dari output dengan multiple fallback strategies.
    """
    if not output or not isinstance(output, str):
        return None
    
    output = output.strip()
    
    # Strategy 1: Direct JSON parse
    try:
        parsed = json.loads(output)
        if isinstance(parsed, list):
            if len(parsed) == 1 and isinstance(parsed[0], dict):
                return parsed[0]
            return {"raw_data": parsed}
        return parsed
    except json.JSONDecodeError:
        pass
    
    # Strategy 2: Find JSON block in output
    try:
        start_idx = output.rfind('{')
        end_idx = output.rfind('}')
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = output[start_idx:end_idx + 1]
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                if len(parsed) == 1 and isinstance(parsed[0], dict):
                    return parsed[0]
                return {"raw_data": parsed}
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Strategy 3: Find JSON array
    try:
        start_idx = output.rfind('[')
        end_idx = output.rfind(']')
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = output[start_idx:end_idx + 1]
            parsed = json.loads(json_str)
            if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                return parsed[0]
            return {"raw_data": parsed}
    except (json.JSONDecodeError, ValueError):
        pass
    
    logger.warning("Failed to parse JSON from output")
    return None

# Check if training modules are available (lightweight check)
def _training_modules_available() -> bool:
    """Check if training modules dependencies are available."""
    try:
        import fitz  # PyMuPDF - required by loader.py
        import sentence_transformers  # required for embeddings
        return True
    except ImportError:
        return False

# Cache the check result
_TRAINING_MODULES_OK = None

def training_modules_available() -> bool:
    """Cached check for training module availability."""
    global _TRAINING_MODULES_OK
    if _TRAINING_MODULES_OK is None:
        _TRAINING_MODULES_OK = _training_modules_available()
        if not _TRAINING_MODULES_OK:
            logger.info("Training modules not available - using direct AI method")
    return _TRAINING_MODULES_OK

# Direct Import Methods (FASTEST - only if dependencies available)

def get_optimized_module():
    """Lazy import optimized module."""
    global _optimized_module
    
    if not training_modules_available():
        raise ImportError("Training module dependencies not installed")
    
    if _optimized_module is None:
        if str(TRAINING_SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(TRAINING_SCRIPTS_DIR))
        
        try:
            import prompt_and_verify as pv
            _optimized_module = pv
            logger.info("✅ Loaded verification module (DeepSeek)")
        except ImportError as e:
            raise ImportError(f"Cannot import verification module: {e}")
    
    return _optimized_module

def call_ai_verify_direct_optimized(claim_text: str) -> Dict[str, Any]:
    """Call AI verification directly."""
    start_time = time.time()
    
    try:
        logger.info(f"🚀 Verifying: {claim_text[:80]}...")
        
        pvo = get_optimized_module()
        
        # Use verify_claim_local (main verification function)
        if hasattr(pvo, 'verify_claim_local'):
            raw_result = pvo.verify_claim_local(
                claim=claim_text,
                k=10,
                dry_run=False,
                enable_expansion=True,
                min_relevance=0.25,
                force_dynamic_fetch=False,
                debug_retrieval=False
            )
        else:
            raise AttributeError("verify_claim_local not found in module")
        
        elapsed = time.time() - start_time
        
        logger.info(f"✅ Verification completed in {elapsed:.1f}s")
        
        # Extract from _frontend_payload if present (new format)
        if "_frontend_payload" in raw_result:
            payload = raw_result["_frontend_payload"]
            logger.debug(f"[PARSE] Extracted from _frontend_payload: label={payload.get('label')}")
        else:
            payload = raw_result
        
        # Get label and map it
        raw_label = payload.get("label", "unverified")
        mapped_label = map_ai_label_to_backend(raw_label)
        
        # Get confidence
        raw_confidence = payload.get("confidence")
        confidence = float(raw_confidence) if raw_confidence is not None else 0.0
        
        # Get summary
        summary = payload.get("summary", "") or payload.get("conclusion", "") or ""
        
        # Get sources from evidence or references.
        # trusted=True: payload berasal dari pipeline RAG training (knowledge base
        # + indeks vektor), bukan karangan LLM.
        sources = extract_sources(payload, trusted=True)
        
        logger.info(f"[PARSE] Label: {raw_label} -> {mapped_label}, Confidence: {confidence}, Sources: {len(sources)}")
        
        return {
            "label": mapped_label,
            "summary": summary,
            "confidence": confidence,
            "sources": sources,
            "_processing_time": elapsed,
            "_method": "direct_optimized",
            "_claim_text": claim_text,
            "_raw_label": raw_label
        }
        
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"❌ Verification failed: {e}", exc_info=True)
        
        return {
            "label": "unverified",
            "summary": f"Verification error: {str(e)[:100]}",
            "confidence": None,
            "sources": [],
            "_error": True,
            "_processing_time": elapsed
        }
        
def call_ai_verify_subprocess(claim_text: str) -> Dict[str, Any]:
    """
    Subprocess fallback method - fallback implementation.
    """
    raise NotImplementedError("Subprocess method not fully implemented - use direct method")
# Public API

def call_ai_verify(claim_text: str, additional_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Public API untuk verifikasi claim.
    Otomatis fallback ke method lain jika script tidak ditemukan.
    """
    claim_text = normalize_claim_text(claim_text)
    
    logger.info(f"🔍 Verifying claim: {claim_text[:100]}...")
    
    # Skip optimized methods if training modules not available (Railway production)
    if not training_modules_available():
        logger.info("Using direct AI call method (training modules not available)")
        result = call_ai_direct(claim_text, additional_evidence)
        return normalize_ai_response(result, claim_text)
    
    # Method 1: Direct import (FASTEST) - jika script ada dan modules tersedia
    if VERIFY_SCRIPT.exists():
        try:
            result = call_ai_verify_direct_optimized(claim_text)
            if result and result.get('label'):
                return normalize_ai_response(result, claim_text)
        except Exception as e:
            logger.warning(f"Direct import failed: {e}, trying subprocess...")
    
    # Method 2: Subprocess (jika script ada tapi import gagal)
    if VERIFY_SCRIPT.exists():
        try:
            result = call_ai_verify_subprocess(claim_text)
            if result and result.get('label'):
                return normalize_ai_response(result, claim_text)
        except Exception as e:
            logger.warning(f"Subprocess failed: {e}, using direct AI call...")
    
    # Method 3: Direct AI call (FALLBACK - SELALU TERSEDIA)
    logger.info("Using direct AI call method")
    result = call_ai_direct(claim_text, additional_evidence)
    return normalize_ai_response(result, claim_text)

def retrieve_grounding_evidence(claim_text: str, limit: int = 8) -> List[Dict[str, Any]]:
    """
    Ambil evidence NYATA dari knowledge base Healthify untuk menjadi grounding
    verifikasi klaim.

    Memakai ulang seluruh sumber pengetahuan yang sudah ada (JournalArticle,
    Source/ClaimSource, dan indeks pgvector bila tersedia) melalui
    `api.intelligence.retrieval`. Tidak ada sumber baru yang dibuat di sini.
    """
    try:
        from .intelligence.contracts import EvidenceStatus
        from .intelligence.evidence.selector import select_evidence
        from .intelligence.retrieval.acquisition import (
            build_topic_phrase,
            coverage_is_thin,
            ensure_coverage,
        )
        from .intelligence.retrieval.concepts import extract_health_concepts
        from .intelligence.retrieval.retriever import retrieve_candidates

        terms = extract_health_concepts(claim_text)
        candidates = retrieve_candidates(claim_text, extra_terms=terms)
        selected, status = select_evidence(candidates, context_terms=terms, limit=limit)

        # Klaim kesehatan yang tidak menemukan bukti berarti topiknya belum
        # terwakili di basis pengetahuan. Melengkapi sendiri lalu mencoba
        # sekali lagi jauh lebih berguna daripada melabeli klaim yang jelas
        # benar sebagai "tidak pasti" hanya karena bahan bacaannya belum ada.
        thin = (status == EvidenceStatus.INSUFFICIENT_EVIDENCE
                or coverage_is_thin(claim_text, selected))
        if thin:
            ensure_coverage(claim_text)
            # Lihat catatan yang sama di engine: percobaan ulang memakai istilah
            # Inggris, dan berjalan baik ada jurnal baru maupun tidak.
            topic = build_topic_phrase(claim_text)
            if topic:
                retry_terms = list(terms) + topic.split()
                candidates = retrieve_candidates(claim_text, extra_terms=retry_terms)
                selected, status = select_evidence(
                    candidates, context_terms=terms, limit=limit)

        logger.info(
            "[GROUNDING] %d evidence terpilih dari knowledge base (status=%s)",
            len(selected), status.value,
        )
        return [
            {
                "title": item.title,
                "doi": item.doi,
                "url": item.url,
                "snippet": item.snippet[:900],
                "authors": item.authors,
                "publisher": item.publisher,
                "relevance_score": item.relevance,
                "source_type": item.source_type,
                "_trusted": True,
            }
            for item in selected
        ]
    except Exception as e:
        logger.warning(f"[GROUNDING] Retrieval knowledge base gagal: {e}")
        return []


def call_ai_direct(claim_text: str, additional_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Direct call ke AI API tanpa menggunakan training script.
    Ini adalah fallback method yang selalu tersedia.

    PERBAIKAN PENTING (anti-halusinasi sumber):
        Sebelumnya LLM diminta ikut menuliskan daftar `sources` — inilah asal
        DOI karangan yang berujung 404. Sekarang:

        1. Evidence diambil DULU dari knowledge base Healthify.
        2. LLM hanya menilai klaim TERHADAP evidence tersebut.
        3. Daftar `sources` yang dikembalikan berasal dari evidence nyata,
           BUKAN dari keluaran LLM.
        4. Bila tidak ada evidence sama sekali, sistem tidak menebak:
           label langsung `unverified`.
    """
    import os
    from openai import OpenAI

    evidence = retrieve_grounding_evidence(claim_text)

    # Tanpa evidence, sistem TIDAK meminta LLM menebak (lihat §16).
    if not evidence:
        logger.info("[VERIFY] Tidak ada evidence di knowledge base -> unverified")
        return {
            'label': 'unverified',
            'confidence': None,
            'summary': (
                'Belum ditemukan sumber ilmiah yang relevan di basis pengetahuan '
                'Healthify untuk memverifikasi klaim ini. Sistem tidak memberikan '
                'kesimpulan tanpa bukti pendukung.'
            ),
            'sources': []
        }

    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        logger.error("OPENAI_API_KEY not set")
        return {
            'label': 'unverified',
            'confidence': None,
            'summary': 'API key not configured',
            'sources': evidence,
        }

    client = OpenAI(api_key=api_key)

    evidence_block = "\n\n".join(
        f"[E{idx}] {item['title']}\n{item['snippet']}"
        for idx, item in enumerate(evidence, start=1)
    )

    prompt = f"""Kamu adalah ahli verifikasi klaim kesehatan. Nilai klaim berikut HANYA berdasarkan EVIDENCE yang diberikan.

Klaim: "{claim_text}"

EVIDENCE (satu-satunya sumber yang boleh dipakai):
{evidence_block}

Aturan mutlak:
- DILARANG menyebut, mengarang, atau menambahkan sumber, DOI, URL, nama jurnal, penulis, atau tahun yang tidak ada di EVIDENCE di atas.
- Jangan menuliskan daftar pustaka. Sumber ditampilkan terpisah oleh sistem.
- Jika EVIDENCE tidak membahas hubungan yang diklaim, gunakan label "uncertain".

Berikan respons dalam format JSON:
{{
    "label": "valid|hoax|uncertain",
    "confidence": 0.0-1.0,   // seberapa yakin klaim ini BENAR (bukan seberapa yakin pada penilaianmu). Untuk "uncertain" isi sekitar 0.5.
    "summary": "Penjelasan 2-5 kalimat dalam bahasa Indonesia untuk pembaca awam. Tulis seperti penjelasan biasa: JANGAN menyebut kata \"EVIDENCE\", \"E1\", \"sumber yang diberikan\", atau membahas proses penilaian. Bila merujuk sumber, letakkan penanda [E1]/[E2] di AKHIR kalimat. Jangan menulis nama jurnal, penulis, atau tahun."
}}

Panduan label:
- "valid": EVIDENCE secara langsung mendukung klaim
- "hoax": EVIDENCE secara langsung membantah klaim
- "uncertain": EVIDENCE tidak cukup atau tidak membahas klaim ini"""

    try:
        from .intelligence.reasoning.llm import completion_token_kwargs, openai_model

        model = openai_model()
        response = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.2,
            response_format={'type': 'json_object'},
            **completion_token_kwargs(model, 2048),
        )

        # Parse JSON dari response
        import json
        result_text = response.choices[0].message.content.strip()
        
        # Hapus markdown code block jika ada
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
        
        result = json.loads(result_text.strip())

        # Sumber SELALU berasal dari knowledge base, tidak pernah dari LLM.
        result['sources'] = evidence
        result['summary'] = strip_fabricated_references(result.get('summary', ''), evidence)
        return result
        
    except Exception as e:
        logger.error(f"Direct AI call failed: {e}")
        # Return minimal valid response
        return {
            'label': 'unverified',
            'confidence': None,
            'summary': f'Unable to verify claim due to technical error: {str(e)}',
            'sources': evidence,
        }


_URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DOI_IN_TEXT_RE = re.compile(r"\b(?:doi:\s*)?10\.\d{4,9}/\S+", re.IGNORECASE)


def strip_fabricated_references(text: str, evidence: List[Dict[str, Any]]) -> str:
    """
    Buang URL/DOI apa pun yang ditulis LLM di badan ringkasan dan tidak ada di
    daftar evidence. Ini menutup celah terakhir DOI karangan bocor ke user.
    """
    allowed_dois = {(e.get('doi') or '').lower() for e in (evidence or []) if e.get('doi')}
    allowed_urls = {(e.get('url') or '').lower() for e in (evidence or []) if e.get('url')}

    def keep_url(match):
        return match.group(0) if match.group(0).lower() in allowed_urls else ""

    def keep_doi(match):
        normalized = re.sub(r"^doi:\s*", "", match.group(0), flags=re.IGNORECASE).lower()
        return match.group(0) if normalized in allowed_dois else ""

    cleaned = _URL_IN_TEXT_RE.sub(keep_url, text or "")
    cleaned = _DOI_IN_TEXT_RE.sub(keep_doi, cleaned)
    cleaned = re.sub(r"\(\s*[,;]?\s*\)", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()

def call_ai_verify_with_evidence(claim_text: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verifikasi klaim dengan evidence tambahan dari user dispute.
    Evidence akan di-inject ke dalam prompt sebagai bukti prioritas.
    """
    start_time = time.time()
    
    try:
        module = get_optimized_module()
        if module is None:
            raise ImportError("Could not import verification module")
        
        # Build custom evidence context
        evidence_context = f"""
=== BUKTI TAMBAHAN DARI USER ===
Judul: {evidence.get('title', 'N/A')}
DOI: {evidence.get('doi', 'N/A')}
URL: {evidence.get('url', 'N/A')}

Abstrak/Konten:
{evidence.get('abstract', 'Tidak tersedia')}
================================
"""
        
        # Gabungkan claim dengan evidence untuk verification
        enhanced_claim = f"{claim_text}\n\n[KONTEKS TAMBAHAN - BUKTI DARI PELAPOR]\n{evidence_context}"
        
        logger.info(f"[VERIFY_WITH_EVIDENCE] Running verification with user evidence...")
        
        # Call verify function dengan enhanced claim
        raw_result = module.verify_claim_local(
            enhanced_claim,
            k=8,  # Lebih banyak neighbors
            dry_run=False,
            enable_expansion=True,
            min_relevance=0.2,  # Lower threshold untuk include evidence
            force_dynamic_fetch=False,
            debug_retrieval=False
        )
        
        elapsed = time.time() - start_time
        logger.info(f"[VERIFY_WITH_EVIDENCE] Completed in {elapsed:.2f}s")
        
        # Add user evidence to sources
        if raw_result.get('sources') is None:
            raw_result['sources'] = []
        
        # Tambahkan evidence user sebagai source pertama
        user_source = {
            'title': evidence.get('title', 'User Provided Evidence'),
            'doi': evidence.get('doi', ''),
            'url': evidence.get('url', ''),
            'relevance_score': 0.95,  # High relevance karena dari user
            '_from_dispute': True
        }
        raw_result['sources'].insert(0, user_source)
        
        return raw_result
        
    except Exception as e:
        logger.error(f"[VERIFY_WITH_EVIDENCE] Error: {e}")
        raise
# Ringkasan konfigurasi saat modul dimuat. Ditulis pada level DEBUG: isinya
# tidak berubah selama proses hidup, tetapi dicetak ulang oleh setiap worker
# pada setiap deploy, dan garis "=" sepanjang delapan puluh karakter membuat
# log sulit dibaca tanpa memberi tahu apa pun yang tidak dapat dilihat dari
# konfigurasi.
logger.debug(
    "[VERIFY] skrip=%s ada=%s timeout=%ss retry=%s",
    VERIFY_SCRIPT, VERIFY_SCRIPT.exists(), VERIFICATION_TIMEOUT, MAX_RETRIES,
)
