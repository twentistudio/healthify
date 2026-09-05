import os
import re
import json
import time
import uuid
import pathlib
import argparse
import sys
import traceback
import re
from typing import List, Dict, Any, Optional
from pathlib import Path

# library ketiga
from dotenv import load_dotenv, find_dotenv
from google import genai
from openai import OpenAI
from psycopg2.extras import RealDictCursor
from pgvector.psycopg2 import register_vector

# project modules
import fetch_sources as fs
import prepare_and_run_loader as pr
import process_raw as praw
import ingest_chunks_to_pg as ic
import chunk_and_embed as cae
from ingest_chunks_to_pg import connect_db, DB_TABLE
from chunk_and_embed import embed_texts_gemini
from fetch_sources import fetch_all_sources

# Cache module
try:
    import cache_manager as cache
    CACHE_ENABLED = True
    print("[INIT] Cache manager loaded", file=sys.stderr)
except ImportError:
    CACHE_ENABLED = False
    print("[INIT] Cache manager not available", file=sys.stderr)

BASE = pathlib.Path(__file__).parent     
TRAINING_DIR = BASE.parent          
PROJECT_ROOT = TRAINING_DIR.parent       

# Memastikan folder data/chunks exists
DATA_DIR = TRAINING_DIR / "data"
CHUNKS_DIR = DATA_DIR / "chunks"

# helper untuk menyimpan hasil verifikasi
VERIFICATION_RESULTS_PATH = TRAINING_DIR / "data" / "metadata" / "verification_results.jsonl"
VERIFICATION_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)

# Pengaturan environment & client 
SCRIPT_DIR = str(BASE)
REPO_ROOT = str(PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

def load_environment_variables():
    """Load environment variables dengan fallback ke multiple lokasi."""
    dotenv_locations = [
        TRAINING_DIR / ".env",
        PROJECT_ROOT / ".env",
        find_dotenv()
    ]
    
    loaded = False
    for dotenv_path in dotenv_locations:
        if dotenv_path and pathlib.Path(dotenv_path).exists():
            load_dotenv(dotenv_path=dotenv_path, override=True)
            loaded = True
            print(f"[ENV] Loaded environment from: {dotenv_path}", file=sys.stderr)
            break
    
    if not loaded:
        print("[ENV] WARNING: No .env file found in standard locations", file=sys.stderr)
        # Mencoba load dari environment yang sudah ada
        load_dotenv()
    
    return loaded

# Load environment variables
load_environment_variables()

# Initialize Gemini client for embeddings 
try:
    gemini_client = getattr(cae, "client", None)
except Exception as e:
    print(f"[INIT] Warning: Could not get client from cae module: {e}", file=sys.stderr)
    gemini_client = None

if gemini_client is None:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        print(f"[WARNING] GEMINI_API_KEY not found - Gemini features disabled (using fallbacks)", file=sys.stderr)
        print(f"[INFO] Translation will pass-through, embeddings will use sentence-transformers", file=sys.stderr)
    else:
        try:
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            print("[INIT] Gemini client initialized for embeddings & translation", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] Failed to initialize Gemini client: {e}", file=sys.stderr)
            print(f"[INFO] Will use fallback methods", file=sys.stderr)

# LLM Provider Configuration (Groq / DeepSeek / OpenRouter)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
# LLM_PROVIDER kini juga dipakai backend (api/intelligence/reasoning/llm.py).
# Nilai "openai" harus dikenali di sini agar satu variabel berlaku konsisten
# untuk backend maupun pipeline training.
LLM_API_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("GROQ_API_KEY")
    or os.getenv("DEEPSEEK_API_KEY")
    or (os.getenv("OPENAI_API_KEY") if LLM_PROVIDER.startswith("openai") else None)
)
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_MODEL = os.getenv("LLM_MODEL")

# Default settings per provider
PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.4-mini"
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.1-8b-instant"  
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat"
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.1-8b-instruct"
    }
}

if not LLM_API_KEY:
    error_msg = (
        f"LLM API Key tidak ditemukan di environment variables.\n"
        f"Provider: {LLM_PROVIDER}\n"
        f"Lokasi .env yang dicek:\n"
        f"  1. {TRAINING_DIR / '.env'}\n"
        f"  2. {PROJECT_ROOT / '.env'}\n"
        f"Tambahkan salah satu:\n"
        f"  - LLM_API_KEY=your_key\n"
        f"  - GROQ_API_KEY=gsk_xxx (untuk Groq)\n"
        f"  - DEEPSEEK_API_KEY=sk_xxx (untuk DeepSeek)"
    )
    print(f"[ERROR] {error_msg}", file=sys.stderr)
    sys.exit(2)

# Apply defaults if not specified
# Backend memperbolehkan LLM_PROVIDER berisi daftar ("openai,gemini");
# di sini hanya provider pertama yang relevan.
_primary_provider = LLM_PROVIDER.split(",")[0].strip() or "groq"

if not LLM_BASE_URL:
    LLM_BASE_URL = PROVIDER_DEFAULTS.get(_primary_provider, {}).get("base_url", "https://api.groq.com/openai/v1")
if not LLM_MODEL:
    LLM_MODEL = PROVIDER_DEFAULTS.get(_primary_provider, {}).get("model", "llama-3.1-8b-instant")

try:
    llm_client = OpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL
    )
    print(f"[INIT] LLM client initialized: {LLM_PROVIDER.upper()} ({LLM_MODEL})", file=sys.stderr)
    print(f"[INIT]   Base URL: {LLM_BASE_URL}", file=sys.stderr)
except Exception as e:
    print(f"[ERROR] Failed to initialize LLM client: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(2)

# Backward compatibility alias
deepseek_client = llm_client

# DOI & Paper Quality Filtering
def is_valid_academic_doi(doi: str, url: str = "") -> bool:
    """
    Filter DOI untuk memastikan hanya paper ilmiah, bukan berita atau blog.
    
    Returns True hanya jika DOI mengarah ke:
    - Journal articles (peer-reviewed)
    - Conference papers  
    - Academic books/chapters
    - Preprints dari repositori terpercaya
    
    Returns False untuk:
    - News articles / journalism
    - Blog posts
    - Magazine articles
    - Non-academic websites
    """
    if not doi:
        return False
    
    doi = doi.strip().lower()
    
    # Publisher DOI prefix yang bukan academic papers
    journalism_publishers = [
        "10.1038/d",  
        "10.1126/science.a", 
        "10.1136/bmj.",  
        "10.1080/14753",
    ]
    
    for prefix in journalism_publishers:
        if doi.startswith(prefix):
            return False
    
    # Trusted academic publishers & repositories
    academic_publishers = [
        "10.1001/",    
        "10.1016/",    
        "10.1038/s",   
        "10.1038/nj",  
        "10.1126/sci", 
        "10.1056/",    
        "10.1136/",    
        "10.1371/",    
        "10.1186/",    
        "10.3389/",    
        "10.1080/",    
        "10.1111/",    
        "10.1093/",    
        "10.1017/",    
        "10.1097/",    
        "10.1002/",    
        "10.1007/",    
        "10.1101/",    
        "10.15252/",   
        "10.1073/",    
        "10.1021/",    
        "10.1039/",    
        "10.1088/",    
        "10.1103/",    
        "10.1109/",    
        "10.1145/",    
        "10.48550/",   
    ]
    
    for prefix in academic_publishers:
        if doi.startswith(prefix):
            return True
    
    # Check URL patterns for additional verification
    if url:
        url_lower = url.lower()
        
        # Definite news/journalism patterns
        news_patterns = [
            "/news/", "/article/", "/blog/", "/opinion/",
            "theconversation.com", "medium.com", "forbes.com",
            "/press-release", "/magazine/", "/newsletter/"
        ]
        
        for pattern in news_patterns:
            if pattern in url_lower:
                return False
        
        # Academic repository/journal patterns
        academic_patterns = [
            "sciencedirect.com/science/article",
            "nature.com/articles/s",
            "ncbi.nlm.nih.gov/pmc",
            "pubmed.ncbi.nlm.nih.gov",
            "scholar.google",
            "arxiv.org/abs",
            "biorxiv.org/content",
            "medrxiv.org/content",
            "journals.",
            "/journal/",
        ]
        
        for pattern in academic_patterns:
            if pattern in url_lower:
                return True
    
    
    if re.match(r'^10\.\d{4,}/\S+', doi):
        return True
    
    return False


def filter_academic_neighbors(neighbors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filter neighbors untuk hanya menyertakan paper akademik berkualitas.
    Removes berita jurnalistik, blog, dan sumber non-peer-reviewed.
    """
    filtered = []
    
    for nb in neighbors:
        doi = nb.get("doi", "")
        url = nb.get("url", "") or nb.get("source_url", "")
        
        # Jika ada DOI, validate
        if doi:
            if is_valid_academic_doi(doi, url):
                nb["_is_academic"] = True
                filtered.append(nb)
            else:
                print(f"[FILTER] Rejected non-academic DOI: {doi}", file=sys.stderr)
        # Jika tidak ada DOI, check URL
        elif url:
            # More lenient for non-DOI sources dari database
            if any(pattern in url.lower() for pattern in [
                "pubmed", "pmc", "arxiv", "biorxiv", "medrxiv", 
                "sciencedirect", "springer", "wiley"
            ]):
                nb["_is_academic"] = True
                filtered.append(nb)
            else:
                print(f"[FILTER] Rejected source without valid DOI/URL: {url[:100]}", file=sys.stderr)
        else:
            # No DOI and no URL - keep if from trusted DB but mark
            nb["_is_academic"] = False
            filtered.append(nb)
    
    return filtered


# Better database connection check dengan proper error handling
def check_database_connection():
    """Check database connection dengan error handling yang lebih baik."""
    try:
        conn = connect_db()
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {DB_TABLE};")
            cnt = cur.fetchone()
            print(f"[DB] Rows in embeddings table: {cnt[0] if cnt else 0}", file=sys.stderr)
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] WARNING: Database connection check failed: {str(e)}", file=sys.stderr)
        print("[DB] Verification will continue but may fail if database is required", file=sys.stderr)
        return False

# Check database pada startup
db_available = check_database_connection()

# Konstanta konfigurasi
PROMPT_HEADER = """
Anda adalah sistem verifikasi fakta medis berbasis jurnal ilmiah. Tugas Anda adalah menilai apakah sebuah klaim kesehatan benar, salah, atau sebagian benar berdasarkan bukti ilmiah yang secara langsung membahas hubungan X → Y dalam klaim tersebut.

X = faktor penyebab atau perilaku.
Y = dampak atau kondisi medis.
Fokuskan seluruh analisis pada hubungan kausal X → Y, bukan pembahasan umum tentang Y saja.
"""

LLM_CONF_THRESHOLD = 0.75
COMBINED_CONF_THRESHOLD = 0.75

# Konfigurasi untuk ekspansi pola relevansi berbasis LLM
EXPANSION_MAX_TERMS = 12
EXPANSION_MIN_TERMS = 3
EXPANSION_MODEL = "gemini-2.5-flash"
EXPANSION_TIMEOUT = 6.0

# Fungsi utilitas umum
def extract_text_from_model_resp(resp) -> str:
    """Ekstrak teks dari berbagai format respons Gemini API."""
    try:
        if resp is None:
            return ""
        
        if hasattr(resp, "text"):
            return resp.text or ""
            
        if hasattr(resp, "candidates") and resp.candidates:
            cand = resp.candidates[0]
            if hasattr(cand, "content"):
                content = cand.content
                if hasattr(content, "parts") and content.parts:
                    part = content.parts[0]
                    return getattr(part, "text", "") or ""
                elif hasattr(content, "text"):
                    return content.text or ""
            return getattr(cand, "text", "") or ""
            
        if isinstance(resp, dict):
            if "candidates" in resp and resp["candidates"]:
                cand = resp["candidates"][0]
                content = cand.get("content", {})
                if isinstance(content, dict) and "parts" in content and content["parts"]:
                    return content["parts"][0].get("text", "") or ""
                if isinstance(cand, dict) and "output" in cand:
                    return cand["output"]
            return str(resp.get("text", "")) if isinstance(resp, dict) else str(resp)
        
        return str(resp)
    except Exception:
        try:
            return str(resp)
        except Exception:
            return ""

def safe_strip(s) -> str:
    """Konversi ke string dan strip dengan aman."""
    try:
        if s is None:
            return ""
        return str(s).strip()
    except Exception:
        return ""

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

def _safe_parse_json_array(text: str) -> List[str]:
    """Parser JSON array yang toleran: jika LLM mengembalikan plaintext, coba parsing per baris."""
    text = text.strip()
    if not text:
        return []
    
    # Coba parsing JSON dulu
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [safe_strip(p) for p in parsed if isinstance(p, str)]
    except Exception:
        pass

    # Coba ekstrak array dalam kurung siku dari teks
    m = re.search(r'\[.*\]', text, flags=re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return [safe_strip(p) for p in parsed if isinstance(p, str)]
        except Exception:
            pass

    # pisah berdasarkan baris dan koma
    candidates = []
    for part in re.split(r'[\n\r,;]+', text):
        part = part.strip().lstrip("-•* ").strip()
        if part:
            candidates.append(part)
    return candidates

def expand_relevance_patterns(claim: str, force_refresh: bool = False) -> List[str]:
    """Generate pola relevansi secara ringan TANPA LLM.

    - Ambil kata kunci penting dari klaim
    - Tambahkan sedikit istilah medis umum sebagai fallback
    - Kembalikan list lowercase untuk dipakai di compute_relevance_score
    """
    claim_clean = safe_strip(claim).lower()
    tokens = re.split(r"[^a-zA-Z0-9]+", claim_clean)
    tokens = [t for t in tokens if len(t) > 3]

    base_terms: List[str] = []
    for t in tokens:
        if t not in base_terms:
            base_terms.append(t)

    # Menambahkan beberapa istilah medis umum sebagai fallback
    medical_fallback = [
        "medical research", "clinical study", "dermatology", "skin care",
        "health effects", "scientific evidence", "medical journal",
        "randomized trial", "systematic review", "meta analysis"
    ]

    patterns_norm: List[str] = []
    for p in base_terms + medical_fallback:
        p2 = re.sub(r"\s+", " ", p).strip().lower()
        if p2 and p2 not in patterns_norm and len(p2) > 2:
            patterns_norm.append(p2)
        if len(patterns_norm) >= EXPANSION_MAX_TERMS:
            break

    if not patterns_norm:
        patterns_norm = ["medical research", "clinical study", "health effects"][:EXPANSION_MIN_TERMS]

    return patterns_norm


def extract_xy_from_claim(claim: str) -> tuple[str, str]:
    """Heuristik sederhana untuk mengekstrak X (penyebab) dan Y (akibat) dari klaim.

    Contoh klaim yang didukung:
    - "X menyebabkan Y"
    - "X dapat menyebabkan Y"
    - "X bisa menyebabkan Y"
    - "X meningkatkan risiko Y"
    - "X causes Y / X can cause Y / X may cause Y / X increases risk of Y"
    """
    claim_clean = safe_strip(claim).lower()
    if not claim_clean:
        return "", ""

    separators = [
        r"dapat menyebabkan",
        r"bisa menyebabkan",
        r"menyebabkan",
        r"meningkatkan risiko",
        r"meningkatkan resiko",
        r"memicu",
        r"mengakibatkan",
        r"can cause",
        r"may cause",
        r"causes",
        r"leads to",
        r"increases risk of",
    ]

    for sep in separators:
        parts = re.split(sep, claim_clean, maxsplit=1)
        if len(parts) == 2:
            x_raw, y_raw = parts[0].strip(" ,.-"), parts[1].strip(" ,.-")
            return x_raw, y_raw

    return "", ""

# Scoring relevansi yang dimodifikasi
def compute_relevance_score(claim: str, neighbor_text: str, neighbor_title: str = "") -> float:
    """
    Hitung skor relevansi menggunakan pola hasil LLM.
    Score = proporsi pola yang muncul di text/title (dibatasi 0..1).
    Juga kombinasikan sedikit keyword matching tradisional untuk kata-kata domain penting.
    """
    claim_lower = safe_strip(claim).lower()
    text_lower = (safe_strip(neighbor_text) + " " + safe_strip(neighbor_title)).lower()

    # Dapatkan pola keyword umum dari klaim
    try:
        patterns = expand_relevance_patterns(claim)
    except Exception:
        patterns = []

    # Pastikan pola unik dan pendek
    patterns = [p.strip().lower() for p in patterns if p and len(p) <= 60]
    patterns = list(dict.fromkeys(patterns))  # preserve order, unique

    # Jika daftar pola kosong karena alasan apapun, fallback ke heuristik minimal
    if not patterns:
        patterns = ["kulit", "wajah", "uap", "vapor", "hidrasi"]

    hit_count = 0
    for p in patterns:
        # pemeriksaan word boundary untuk token pendek untuk menghindari kecocokan substring yang tidak disengaja
        if len(p.split()) == 1:
            if re.search(r'\b' + re.escape(p) + r'\b', text_lower):
                hit_count += 1
        else:
            if p in text_lower:
                hit_count += 1

    score = hit_count / max(len(patterns), 1)

    # Soft cap & smoothing: jika sangat sedikit pola yang cocok tapi teks berisi token klaim, tingkatkan sedikit
    if score < 0.2 and any(tok for tok in re.findall(r"[A-Za-z0-9À-ÿ]{3,}", claim_lower) if tok in text_lower):
        score = min(score + 0.15, 1.0)

    try:
        x_raw, y_raw = extract_xy_from_claim(claim)
    except Exception:
        x_raw, y_raw = "", ""

    if x_raw or y_raw:
        def _tokens(s: str) -> List[str]:
            return [t for t in re.split(r"[^a-z0-9À-ÿ]+", s.lower()) if len(t) > 3]

        x_tokens = _tokens(x_raw)
        y_tokens = _tokens(y_raw)

        def _has_tokens(tokens: List[str]) -> bool:
            return any(re.search(r"\\b" + re.escape(tok) + r"\\b", text_lower) for tok in tokens)

        has_x = _has_tokens(x_tokens) if x_tokens else False
        has_y = _has_tokens(y_tokens) if y_tokens else False

        if has_x and has_y:
            # Dokumen eksplisit membahas X dan Y -> kuatkan skor
            score = min(score * 1.5 + 0.2, 1.0)
        elif has_y and not has_x:
            # Hanya bicara Y tanpa X -> turunkan prioritas tapi tetap bisa muncul
            score = min(score * 0.4, 0.5)
        elif has_x and not has_y:
            # Hanya bicara X (penyebab) tanpa akibat Y -> relevan tapi lemah
            score = min(score * 0.6, 0.6)
        else:
            # Tidak jelas menyebut X maupun Y spesifik -> biarkan skor apa adanya
            pass

    return float(max(0.0, min(1.0, score)))


# Fungsi retrieval dan filtering
def safe_cosine_similarity(a: List[float], b: List[float]) -> float:
    """Hitung cosine similarity antara dua vektor secara aman."""
    try:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return dot / (norm_a * norm_b)
    except Exception:
        return 0.0

def distance_to_similarity(dist: Optional[float]) -> float:
    """Konversi metrik jarak ke skor similaritas."""
    if dist is None:
        return 0.0
    try:
        return 1.0 / (1.0 + float(dist))
    except Exception:
        return 0.0

def retrieve_neighbors_from_db(query_embedding: List[float], k: int = 5, 
                              max_chars_each: int = 2000, 
                              debug_print: bool = False) -> List[Dict[str, Any]]:
    """Ambil k tetangga terdekat dari database menggunakan vektor similaritas."""
    conn = connect_db()
    try:
        try:
            register_vector(conn)
        except Exception:
            pass

        emb_str = "[" + ",".join(str(float(x)) for x in query_embedding) + "]"
        sql = f"""
            SELECT doc_id, safe_id, source_file, chunk_index, n_words, text, doi,
                   embedding <-> %s::vector AS distance
            FROM {DB_TABLE}
            WHERE embedding IS NOT NULL
            ORDER BY distance
            LIMIT %s;
        """

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (emb_str, k))
            rows = cur.fetchall()
    finally:
        conn.close()

    if debug_print and rows:
        print(f"[DEBUG] Berhasil mengambil {len(rows)} baris, DOI pertama: {rows[0].get('doi', 'Tidak ada DOI')}")

    results = []
    for r in rows:
        txt = safe_strip((r.get("text") or "").replace("\n", " "))[:max_chars_each]
        dist_val = None
        try:
            raw_dist = r.get("distance")
            dist_val = float(raw_dist) if raw_dist is not None else None
        except Exception:
            pass

        results.append({
            "doc_id": r.get("doc_id"),
            "safe_id": r.get("safe_id"),
            "source_file": r.get("source_file"),
            "chunk_index": r.get("chunk_index"),
            "n_words": r.get("n_words"),
            "text": txt,
            "doi": r.get("doi", "") or "",
            "distance": dist_val
        })
    
    return results

def filter_by_relevance(claim: str, neighbors: List[Dict[str, Any]], 
                       min_relevance: float = 0.3, debug: bool = False) -> List[Dict[str, Any]]:
    """Filter tetangga berdasarkan skor relevansi dan urutkan berdasarkan skor gabungan."""
    filtered = []
    
    for nb in neighbors:
        text = nb.get("text", "")
        title = nb.get("title", "") or nb.get("safe_id", "")

        rel_score = compute_relevance_score(claim, text, title)
        dist = nb.get("distance", 1.0)
        dist_score = 1.0 / (1.0 + float(dist)) if dist is not None else 0.5
        combined_score = 0.6 * dist_score + 0.4 * rel_score
        nb["relevance_score"] = combined_score

        if combined_score >= min_relevance:
            filtered.append(nb)
            if debug:
                print(f"[RELEVANSI] Pertahankan dok {nb.get('safe_id')} (skor: {combined_score:.3f})")
        elif debug:
            print(f"[RELEVANSI] Filter dok {nb.get('safe_id')} (skor: {combined_score:.3f})")

    filtered.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return filtered


# Fungsi terjemahan dan query expansion
def translate_text_gemini(text: str, target_lang: str = "English") -> str:
    """Terjemahkan teks menggunakan Gemini API dengan cache."""
    if not text or not gemini_client:
        return text
    
    # Check cache first
    if CACHE_ENABLED:
        cached = cache.get_cached_translation(text, target_lang)
        if cached:
            return cached
    
    prompt = (
        f"Terjemahkan teks berikut ke bahasa {target_lang}. "
        f"Pertahankan keringkasan dan preserve terminologi medis bila memungkinkan.\n\n"
        f"Teks:\n\"\"\"\n{text}\n\"\"\"\n\nOutput hanya teks yang diterjemahkan."
    )
    
    try:
        resp = gemini_client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
            config={"temperature": 0.0, "max_output_tokens": 256}
        )
        txt = extract_text_from_model_resp(resp)
        txt = safe_strip(txt).replace("```", "")
        result = txt or text
        
        # Cache the translation
        if CACHE_ENABLED and result != text:
            cache.cache_translation(text, target_lang, result)
        
        return result
    except Exception as e:
        print(f"[TERJEMAH] gagal: {e}")
        return text

def generate_bilingual_queries(claim: str, langs: List[str] = ["English"], max_queries: int = 4) -> List[str]:
    """Generate variasi query dalam multiple bahasa - PRIORITAS ENGLISH."""
    queries = []
    claim_clean = safe_strip(claim)
    
    # Menerjemahan ke English DULU (untuk jurnal internasional)
    if gemini_client and langs:
        for lang in langs[:1]:  # English translation first
            try:
                translated = translate_text_gemini(claim_clean, target_lang=lang)
                translated = safe_strip(translated)
                if translated and translated != claim_clean:
                    queries.append(translated)  # English query FIRST
                    print(f"[BILINGUAL] English query: {translated[:80]}...")
                    break
            except Exception as e:
                print(f"[BILINGUAL] Translation failed: {e}")
    
    # Original query (Indonesian/other)
    if claim_clean:
        queries.append(claim_clean)
    
    # Dedupe dan batasi
    unique_queries = []
    for q in queries[:max_queries]:
        qn = safe_strip(q)
        if qn and qn not in unique_queries:
            unique_queries.append(qn)
    
    print(f"[BILINGUAL_QUERIES] Generated {len(unique_queries)} queries (English-first)")
    return unique_queries

def retrieve_with_expansion_bilingual(claim: str, k: int = 5, 
                                     expand_queries: bool = True, 
                                     debug: bool = False) -> List[Dict[str, Any]]:
    """Retrieve dokumen dengan query expansion dalam multiple bahasa."""
    if expand_queries:
        queries = generate_bilingual_queries(claim, langs=["English"])
    else:
        queries = [claim]

    if debug:
        print(f"[BILINGUAL_QUERIES] Menggunakan queries: {queries}")

    all_neighbors = []
    seen_ids = set()
    
    for query in queries:
        try:
            emb = embed_texts_gemini([query])[0]
        except Exception as e:
            print(f"[RETRIEVE_BIL] embed gagal untuk query '{query}': {e}")
            continue

        neighbors = retrieve_neighbors_from_db(emb, k=k, debug_print=debug)
        for nb in neighbors:
            nb_id = f"{nb.get('doc_id')}_{nb.get('chunk_index')}"
            if nb_id not in seen_ids:
                seen_ids.add(nb_id)
                nb["_matched_query"] = query
                all_neighbors.append(nb)

    filtered = filter_by_relevance(claim, all_neighbors, min_relevance=0.25, debug=debug)
    
    # Prioritaskan jurnal berbahasa Inggris (deteksi dari text)
    for nb in filtered:
        text = nb.get("text", "")
        # Simple heuristic: jika text mengandung banyak kata Inggris umum, boost score
        english_indicators = ["the", "and", "of", "in", "to", "a", "is", "that", "for", "with"]
        english_count = sum(1 for word in english_indicators if f" {word} " in text.lower())
        
        if english_count >= 5:  # Likely English text
            nb["relevance_score"] = nb.get("relevance_score", 0) * 1.3  # 30% boost
            nb["_is_english"] = True
            if debug:
                print(f"[ENGLISH_BOOST] {nb.get('safe_id')} boosted (score: {nb['relevance_score']:.3f})")
        else:
            nb["_is_english"] = False
    
    filtered.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return filtered[:k*3]


# Fungsi dynamic fetch dan ingest
def load_fetched_file_to_items(path_or_obj):
    """Load dan parse file hasil fetch ke format items yang standardized."""
    items = []
    
    if isinstance(path_or_obj, dict):
        title = path_or_obj.get("title") or path_or_obj.get("paperTitle") or ""
        abstract = path_or_obj.get("abstract") or path_or_obj.get("summary") or ""
        url = path_or_obj.get("url") or path_or_obj.get("pdf_url") or ""
        doi = path_or_obj.get("doi") or path_or_obj.get("paperId") or ""
        items.append({"title": title, "abstract": abstract, "url": url, "doi": doi})
        return items

    if isinstance(path_or_obj, (list, tuple)):
        for item in path_or_obj:
            items.extend(load_fetched_file_to_items(item))
        return items

    if isinstance(path_or_obj, str):
        p = pathlib.Path(path_or_obj)
        if not p.exists():
            try:
                obj = json.loads(path_or_obj)
                return load_fetched_file_to_items(obj)
            except Exception:
                return []
        
        name = p.name.lower()
        try:
            if name.startswith("crossref") and p.suffix == ".json":
                parsed = praw.parse_crossref_file(p)
                for d in parsed:
                    items.append({
                        "title": d.get("title", ""), 
                        "abstract": d.get("abstract", ""), 
                        "url": d.get("url", "") if d.get("url") else "", 
                        "doi": d.get("doi", "")
                    })
            elif "semantic" in name and p.suffix == ".json":
                parsed = praw.parse_sematic_scholar_file(p)
                for d in parsed:
                    items.append({
                        "title": d.get("title", ""), 
                        "abstract": d.get("abstract", ""), 
                        "url": d.get("url", "") if d.get("url") else "", 
                        "doi": d.get("doi", "")
                    })
            elif "pubmed" in name and p.suffix in [".xml", ".txt"]:
                parsed = praw.parse_pubmed_xml_file(p)
                for d in parsed:
                    items.append({
                        "title": d.get("title", ""), 
                        "abstract": d.get("abstract", ""), 
                        "url": d.get("url", "") if d.get("url") else "", 
                        "doi": d.get("doi", "")
                    })
            # ===== NEW SOURCES =====
            elif name.startswith("europepmc") and p.suffix == ".json":
                if hasattr(praw, 'parse_europepmc_file'):
                    parsed = praw.parse_europepmc_file(p)
                    for d in parsed:
                        items.append({
                            "title": d.get("title", ""), 
                            "abstract": d.get("abstract", ""), 
                            "url": d.get("url", "") if d.get("url") else "", 
                            "doi": d.get("doi", "")
                        })
            elif name.startswith("openalex") and p.suffix == ".json":
                if hasattr(praw, 'parse_openalex_file'):
                    parsed = praw.parse_openalex_file(p)
                    for d in parsed:
                        items.append({
                            "title": d.get("title", ""), 
                            "abstract": d.get("abstract", ""), 
                            "url": d.get("url", "") if d.get("url") else "", 
                            "doi": d.get("doi", "")
                        })
            elif name.startswith("doaj") and p.suffix == ".json":
                if hasattr(praw, 'parse_doaj_file'):
                    parsed = praw.parse_doaj_file(p)
                    for d in parsed:
                        items.append({
                            "title": d.get("title", ""), 
                            "abstract": d.get("abstract", ""), 
                            "url": d.get("url", "") if d.get("url") else "", 
                            "doi": d.get("doi", "")
                        })
            elif name.startswith("arxiv") and p.suffix == ".json":
                if hasattr(praw, 'parse_arxiv_file'):
                    parsed = praw.parse_arxiv_file(p)
                    for d in parsed:
                        items.append({
                            "title": d.get("title", ""), 
                            "abstract": d.get("abstract", ""), 
                            "url": d.get("url", "") if d.get("url") else "", 
                            "doi": d.get("doi", "")
                        })
            else:
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                    return load_fetched_file_to_items(obj)
                except Exception:
                    pass
        except Exception as e:
            print(f"[LOAD_FETCHED] parse error untuk {p}: {e}")
    
    return items

def save_verification_result(result: Dict[str, Any]) -> None:
    """Simpan single verification payload ke JSONL (append)."""
    try:
        with open(VERIFICATION_RESULTS_PATH, "a", encoding="utf-8") as fo:
            fo.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception as e:
        # Tidak menghentikan pipeline jika gagal menyimpan; cukup laporkan
        print(f"[SAVE_VERIF] Gagal menyimpan hasil verifikasi: {e}")

def ingest_abstracts_as_chunks(selected_items: List[Dict[str, Any]]) -> bool:
    """Ingest selected abstracts sebagai chunks ke database."""
    if not selected_items:
        return False

    chuncks_dir = TRAINING_DIR / "data" / "chunks"
    chuncks_dir.mkdir(parents=True, exist_ok=True)

    texts = []
    metas = []

    for item in selected_items:
        text = (item.get("title", "") + "\n\n" + item.get("abstract", "")).strip()
        if not text:
            continue
        
        texts.append(text)
        metas.append({
            "safe_id": item.get("doi") or item.get("url") or str(uuid.uuid4()),
            "doi": item.get("doi", ""),
            "source": item.get("url", "") or item.get("source", "") or "",
        })

    if not texts:
        return False

    try:
        vectors = embed_texts_gemini(texts)
    except Exception as e:
        print(f"[FAST_INGEST] Error embedding abstracts: {e}")
        return False

    out_fname = f"abstract_chunks_{int(time.time())}.jsonl"
    out_path = chuncks_dir / out_fname

    with open(out_path, "w", encoding="utf-8") as fo:
        for text, meta, vec in zip(texts, metas, vectors):
            record = {
                "doc_id": meta["safe_id"],
                "safe_id": meta["safe_id"],
                "source_file": "dynamic_fetch",
                "chunk_index": 0,
                "n_words": len(text.split()),
                "text": text,
                "doi": meta["doi"],
                "source_url": meta.get("source", ""),
                "embedding": list(vec) if hasattr(vec, "__iter__") else vec
            }
            fo.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"[FAST_INGEST] Menulis {len(texts)} abstract chunks ke {out_path}")

    try:
        ic.ingest()
        time.sleep(1.0)
        return True
    except Exception as e:
        print(f"[FAST_INGEST] ingest() gagal: {e}")
        return False

def dynamic_fetch_and_update(claim: str, max_fetch_results: int = 10, 
                            top_k_select: int = 8, use_parallel: bool = True) -> tuple:
    """Fetch dokumen dari multiple sources dan pilih yang paling relevan.
    
    Args:
        claim: Klaim yang akan diverifikasi
        max_fetch_results: Max results per source
        top_k_select: Jumlah dokumen teratas yang dipilih
        use_parallel: Gunakan parallel fetching (LEBIH CEPAT)
    """
    print(f"[DYNAMIC_FETCH] Fetching untuk: {claim[:80]}...")
    fetched_paths = []
    
    # Menerjemahkan ke English untuk fetch dari sumber internasional
    english_claim = claim
    if gemini_client:
        try:
            english_claim = translate_text_gemini(claim, target_lang="English")
            if english_claim and english_claim != claim:
                print(f"[DYNAMIC_FETCH] English query: {english_claim[:80]}...")
        except Exception as e:
            print(f"[DYNAMIC_FETCH] Translation failed: {e}, using original")
            english_claim = claim

    # ALL 7 available sources
    ALL_SOURCES = [
        "pubmed",           
        "europepmc",          
        "openalex",         
        "crossref",         
        "semantic_scholar", 
        "doaj",             
        "arxiv",            
    ]
    
    if use_parallel and hasattr(fs, 'fetch_sources_parallel'):
        # FAST PARALLEL MODE - BILINGUAL 
        print(f"[DYNAMIC_FETCH] Using PARALLEL mode with {len(ALL_SOURCES)} sources...")
        
        try:
            # English query untuk jurnal internasional
            print(f"[DYNAMIC_FETCH] Fetching with ENGLISH query...")
            parallel_results_en = fs.fetch_sources_parallel(
                english_claim,
                sources=ALL_SOURCES,
                limit=max_fetch_results,
                timeout=20
            )
            fetched_paths.extend(list(parallel_results_en.values()))
            print(f"[DYNAMIC_FETCH] English fetch: {len(parallel_results_en)} sources")
            
            # Indonesian query untuk jurnal lokal (jika berbeda)
            if claim != english_claim:
                print(f"[DYNAMIC_FETCH] Fetching with INDONESIAN query...")
                parallel_results_id = fs.fetch_sources_parallel(
                    claim,  # Original Indonesian
                    sources=["crossref", "doaj", "openalex"], 
                    limit=max_fetch_results // 2,
                    timeout=10
                )
                fetched_paths.extend(list(parallel_results_id.values()))
                print(f"[DYNAMIC_FETCH] Indonesian fetch: {len(parallel_results_id)} sources")
            
            print(f"[DYNAMIC_FETCH] Total: {len(fetched_paths)} source files")
        except Exception as e:
            print(f"[DYNAMIC_FETCH] Parallel fetch gagal: {e}, fallback ke sequential")
            use_parallel = False
    
    if not use_parallel or not fetched_paths:
        # SEQUENTIAL MODE (fallback) - BILINGUAL 
        print("[DYNAMIC_FETCH] Using SEQUENTIAL mode...")
        
        # English sources
        sources_en = [
            ("pubmed", lambda: fs.fetch_pubmed(english_claim, maximum_results=max_fetch_results)),
            ("europepmc", lambda: fs.fetch_europe_pmc(english_claim, limit=max_fetch_results)),
            ("openalex", lambda: fs.fetch_openalex(english_claim, limit=max_fetch_results)),
            ("crossref", lambda: fs.fetch_crossref(english_claim, rows=max_fetch_results)),
            ("semantic_scholar", lambda: fs.fetch_semantic_scholar(english_claim, limit=max_fetch_results)),
            ("doaj", lambda: fs.fetch_doaj(english_claim, limit=max_fetch_results)),
            ("arxiv", lambda: fs.fetch_arxiv(english_claim, limit=max_fetch_results)),
        ]
        
        # Indonesian sources (if different query)
        sources_id = []
        if claim != english_claim:
            sources_id = [
                ("crossref_id", lambda: fs.fetch_crossref(claim, rows=max_fetch_results // 2)),
                ("doaj_id", lambda: fs.fetch_doaj(claim, limit=max_fetch_results // 2)),
            ]

        for source_name, fetch_func in sources_en + sources_id:
            try:
                result = fetch_func()
                if result:
                    fetched_paths.append(result)
                time.sleep(0.1)
            except Exception as e:
                print(f"[DYNAMIC_FETCH] {source_name} gagal: {e}")

    # Parse semua fetched items
    all_items = []
    for fp in fetched_paths:
        try:
            items = load_fetched_file_to_items(fp)
            if items:
                all_items.extend(items)
        except Exception as e:
            print(f"[DYNAMIC_FETCH] Error loading fetched file {fp}: {e}")

    if not all_items:
        print("[DYNAMIC_FETCH] Tidak ada items yang tersedia setelah parsing fetched files.")
        return False, []

    # Siapkan teks untuk embedding
    candidate_texts = []
    candidate_meta = []
    
    for item in all_items:
        text = (item.get("title", "") + "\n\n" + (item.get("abstract", "") or "")).strip()
        if text:
            candidate_texts.append(text)
            candidate_meta.append(item)

    if not candidate_texts:
        print("[DYNAMIC_FETCH] Tidak ada candidate texts untuk di-embed.")
        return False, []

    # Pilih items paling relevan menggunakan kombinasi cosine similarity + skor relevansi klaim
    try:
        claim_vec = embed_texts_gemini([claim])[0]
        candidate_vecs = embed_texts_gemini(candidate_texts)
    except Exception as e:
        print(f"[DYNAMIC_FETCH] Embedding gagal: {e}")
        return False, []

    similarities = [safe_cosine_similarity(claim_vec, v) for v in candidate_vecs]

    # Hitung skor relevansi berbasis keyword untuk setiap kandidat, lalu gabungkan
    scored = []
    for i, meta in enumerate(candidate_meta):
        title = meta.get("title", "")
        abstract = meta.get("abstract", "") or ""
        full_text = (title + "\n\n" + abstract).strip()

        rel_score = compute_relevance_score(claim, full_text, title)
        sim_score = similarities[i] if i < len(similarities) else 0.0
        combined = 0.6 * float(sim_score) + 0.4 * float(rel_score)

        scored.append({
            "idx": i,
            "sim": float(sim_score),
            "rel": float(rel_score),
            "combined": float(combined),
        })

    # Urutkan berdasarkan skor gabungan (paling relevan di atas)
    scored.sort(key=lambda x: x["combined"], reverse=True)

    # Buang kandidat yang sangat tidak relevan dengan klaim (combined terlalu rendah)
    MIN_COMBINED = 0.25
    top_scored = [s for s in scored if s["combined"] >= MIN_COMBINED][:top_k_select]

    # Jika semua skor di bawah threshold, tetap ambil beberapa teratas agar pipeline tetap jalan
    if not top_scored:
        top_scored = scored[:top_k_select]

    selected_indices = [s["idx"] for s in top_scored]
    selected = [candidate_meta[i] for i in selected_indices]

    print(
        f"[DYNAMIC_FETCH] Terpilih {len(selected)} items "
        f"(top combined: {[round(s['combined'], 3) for s in top_scored[:5]]})"
    )

    # Normalisasi selected items
    normalized_selected = []
    for s in selected:
        normalized_selected.append({
            "title": s.get("title", ""),
            "abstract": s.get("abstract", ""),
            "url": s.get("url", ""),
            "doi": s.get("doi", "")
        })

    did_ingest = ingest_abstracts_as_chunks(normalized_selected)
    return did_ingest, normalized_selected

def needs_dynamic_fetch(neighbors: List[Dict[str, Any]], claim: str,
                        min_relevance_mean: float = 0.30,
                        min_retriever_mean: float = 0.30,
                        min_max_relevance: float = 0.45,
                        debug: bool = False) -> bool:
    """Tentukan apakah perlu dynamic fetch berdasarkan quality metrics."""
    if not neighbors:
        if debug:
            print("[QUALITY_CHECK] Tidak ada neighbors -> perlu dynamic fetch.")
        return True

    relevance_scores = [n.get("relevance_score", 0.0) for n in neighbors if n.get("relevance_score") is not None]
    similarity_scores = [distance_to_similarity(n.get("distance")) for n in neighbors if n.get("distance") is not None]

    relevance_mean = float(sum(relevance_scores) / len(relevance_scores)) if relevance_scores else 0.0
    retriever_mean = float(sum(similarity_scores) / len(similarity_scores)) if similarity_scores else 0.0
    max_relevance = max(relevance_scores) if relevance_scores else 0.0

    # Periksa keyword overlap
    claim_tokens = set(re.findall(r"[A-Za-zÀ-ÿ0-9]+", safe_strip(claim).lower()))
    
    def has_keyword_match(text: str) -> bool:
        txt_tokens = set(re.findall(r"[A-Za-zÀ-ÿ0-9]+", safe_strip(text).lower()))
        overlap = [t for t in (claim_tokens & txt_tokens) if len(t) > 2]
        return len(overlap) > 0

    any_keyword = any(
        has_keyword_match(n.get("text", "")) or has_keyword_match(n.get("safe_id", "") or "") 
        for n in neighbors
    )

    if debug:
        print(f"[QUALITY_CHECK] relevance_mean={relevance_mean:.3f}, "
              f"retriever_mean={retriever_mean:.3f}, max_rel={max_relevance:.3f}, "
              f"any_keyword={any_keyword}")

    # Logika keputusan
    quality_thresholds_met = (
        relevance_mean >= min_relevance_mean and 
        retriever_mean >= min_retriever_mean and 
        max_relevance >= min_max_relevance and 
        any_keyword
    )

    if not quality_thresholds_met:
        if debug:
            print("[QUALITY_CHECK] Quality thresholds tidak tercapai -> dynamic fetch direkomendasikan")
        return True

    return False

def translate_snippets_if_needed(neighbors: List[Dict[str, Any]], target_lang="Indonesian") -> List[Dict[str, Any]]:
    """Terjemahkan snippet text ke target language jika diperlukan berdasarkan heuristic."""
    for nb in neighbors:
        text = nb.get("text", "")
        if not text:
            nb["_text_translated"] = ""
            continue
        
        # Heuristic: jika banyak English words, translate ke Indonesian
        english_words = len(re.findall(r"[a-zA-Z]{4,}", text))
        indonesian_connectives = len(re.findall(r"\b(dan|yang|dengan|atau|untuk|di)\b", text.lower()))
        
        if english_words > max(5, indonesian_connectives * 3):
            try:
                nb["_text_translated"] = translate_text_gemini(text, target_lang=target_lang)
            except Exception as e:
                print(f"[TRANSLATE] gagal untuk snippet: {e}")
                nb["_text_translated"] = text
        else:
            nb["_text_translated"] = text
    
    return neighbors


def convert_dynamic_to_neighbors(dynamic_selected: List[Dict[str, Any]], claim: str) -> List[Dict[str, Any]]:
    """Konversi hasil dynamic fetch ke format neighbors untuk LLM prompt."""
    fallback_neighbors = []
    for idx, s in enumerate(dynamic_selected, 1):
        text = (s.get("title", "") + "\n\n" + (s.get("abstract", "") or "")).strip()
        if text:
            rel_score = compute_relevance_score(claim, text, s.get("title", ""))
            fallback_neighbors.append({
                "doc_id": s.get("doi") or s.get("url") or f"dyn_{idx}",
                "safe_id": s.get("doi") or s.get("url") or f"dyn_{idx}",
                "source_file": "dynamic_fetch_fallback",
                "chunk_index": 0,
                "n_words": len(text.split()),
                "text": text,
                "doi": s.get("doi", ""),
                "distance": None,
                "relevance_score": float(rel_score),
            })
    return fallback_neighbors



# Fungsi prompt building dan LLM calling
def build_prompt(claim: str, neighbors: List[Dict[str, Any]], max_context_chars: int = 3000) -> str:
    """Buat prompt untuk LLM dengan evidence dari neighbors."""
    parts = []
    total_chars = 0
    
    for idx, nb in enumerate(neighbors, 1):
        doi = nb.get('doi', '') or ''
        safe_id = nb.get('safe_id', '') or nb.get('doc_id', '')
        source_file = nb.get('source_file', '')
        relevance_score = nb.get('relevance_score', 0.0)
        text_for_prompt = nb.get("_text_translated") or nb.get("text", "")
        
        source_info = f"[Bukti #{idx}] ID: {safe_id}"
        if doi:
            source_info += f" | DOI: {doi}"
        if source_file:
            source_info += f" | File: {source_file}"
        source_info += f" | Relevansi: {relevance_score:.3f}"
        
        part = f"{source_info}\n{text_for_prompt}\n---\n"
        if total_chars + len(part) > max_context_chars:
            break
        
        parts.append(part)
        total_chars += len(part)

    context_block = "\n".join(parts)
    
    return f"""{PROMPT_HEADER}

KLAIM:
"{claim.strip()}"

BUKTI ILMIAH:
{context_block}

INSTRUKSI ANALISIS (IKUTI LANGKAH BERIKUT DAN JANGAN LEWATKAN TAHAP APA PUN):

1. Ekstraksi Struktur Klaim:
   - Identifikasi X = faktor penyebab / perilaku utama dalam klaim.
   - Identifikasi Y = dampak / kondisi medis dalam klaim.
   - Fokuskan seluruh analisis pada hubungan kausal X → Y, bukan pembahasan Y secara umum.

2. Validasi Evidence:
   - Gunakan hanya sumber yang secara eksplisit meneliti hubungan X → Y.
   - Abaikan sumber yang hanya membahas Y tanpa menguji pengaruh X.
   - Jika tidak ada bukti langsung untuk X → Y, jelaskan keterbatasan tersebut secara eksplisit.

3. Analisis Kausalitas:
   - Jelaskan apakah ada bukti kuat bahwa X menyebabkan atau meningkatkan risiko Y.
   - Jelaskan jika ada bukti yang membantah hubungan X → Y.
   - Jelaskan jika bukti masih terbatas, tidak langsung, atau hanya observasional.

4. Penetapan Label (pilih hanya satu):
   - VALID: hubungan X → Y didukung kuat oleh penelitian ilmiah.
   - HOAX: penelitian menunjukkan X tidak menyebabkan Y atau klaim menyesatkan.
   - PARTIALLY_VALID: hubungan X → Y hanya benar pada kondisi tertentu, risikonya meningkat tetapi tidak langsung menyebabkan Y, atau bukti tidak cukup kuat.

5. Penetapan Confidence:
   - High (0.75–1.0): banyak bukti langsung dan konsisten.
   - Medium (0.45–0.74): ada sebagian bukti, tetapi tidak konsisten atau terbatas.
   - Low (0.20–0.44): bukti sangat terbatas atau tidak langsung.
   - Konversikan tingkat keyakinan tersebut ke angka 0.0–1.0 pada field "confidence".

6. Buat Summary:
   - Maksimal 100–130 kata.
   - Ringkas, berbasis bukti, tidak ambigu.
   - Fokus pada hubungan X → Y dan bukan edukasi umum yang tidak relevan.

7. Output JSON:
   - Gunakan format berikut dan JANGAN tambahkan teks di luar JSON.

OUTPUT FORMAT:
{{
  "claim": "klaim asli",
  "analysis": "penjelasan terstruktur tentang bagaimana bukti mendukung/menolak hubungan X → Y",
  "label": "VALID" | "HOAX" | "PARTIALLY_VALID",
  "confidence": 0.0,
  "evidence": [
    {{
      "safe_id": "ID atau DOI bukti yang digunakan (opsional, gunakan jika diketahui dari bagian BUKTI ILMIAH)",
      "title": "judul studi utama yang paling relevan",
      "relevance": "penjelasan singkat kenapa studi ini relevan dengan hubungan X → Y",
      "summary": "ringkasan 1–3 kalimat tentang temuan utama studi terkait X → Y"
    }}
  ],
  "summary": "ringkasan 100–130 kata yang menjelaskan secara singkat apakah klaim X → Y valid, hoax, atau sebagian benar dan mengapa."
}}

Pastikan analisis konsisten, tidak kontradiktif, dan tidak mengubah label (VALID/HOAX/PARTIALLY_VALID) di tengah penjelasan.
"""

def call_deepseek(prompt: str, model: str = None, 
               temperature: float = 0.1, max_tokens: int = 3000) -> str:
    """Panggil LLM API (Groq/DeepSeek/OpenRouter) untuk verifikasi klaim kesehatan.
    
    Provider dikonfigurasi via .env:
    - LLM_PROVIDER=groq (default, gratis & cepat)
    - LLM_PROVIDER=deepseek 
    - LLM_PROVIDER=openrouter
    """
    # Use configured model if not specified
    if model is None:
        model = LLM_MODEL
    
    print(f"[LLM] Calling {LLM_PROVIDER.upper()} model: {model}", file=sys.stderr)
    
    try:
        response = llm_client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system", 
                    "content": "You are an expert medical fact-checker with deep knowledge of evidence-based medicine, biomedical research methodology, and clinical practice. You analyze health claims rigorously using scientific literature and provide nuanced, balanced conclusions. Always respond in valid JSON format."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"} if LLM_PROVIDER != "groq" else None
        )
        
        text = response.choices[0].message.content
        text = safe_strip(text)
        
        if not text:
            raise ValueError(f"{LLM_PROVIDER} returned empty response")
        
        # Clean JSON fences if present
        text = text.replace("```json", "").replace("```", "").strip()
        
        print(f"[LLM] Response received ({len(text)} chars)", file=sys.stderr)
        return text
        
    except Exception as e:
        print(f"[ERROR] {LLM_PROVIDER} LLM failed: {str(e)}", file=sys.stderr)
        print(f"[FALLBACK] Trying Gemini as backup LLM...", file=sys.stderr)
        
        # Fallback to Gemini
        try:
            return call_gemini_llm(prompt, temperature, max_tokens)
        except Exception as gemini_error:
            print(f"[ERROR] Gemini fallback also failed: {gemini_error}", file=sys.stderr)
            raise RuntimeError(f"All LLM APIs failed. DeepSeek: {str(e)}, Gemini: {str(gemini_error)}")


def call_gemini_llm(prompt: str, temperature: float = 0.1, max_tokens: int = 2500) -> str:
    """
    Fallback LLM menggunakan Gemini saat DeepSeek gagal.
    """
    if not gemini_client:
        raise RuntimeError("Gemini client not available")
    
    try:
        system_prompt = "You are an expert medical fact-checker. Analyze health claims using scientific evidence. Always respond in valid JSON format with keys: label, confidence, summary, evidence."
        full_prompt = f"{system_prompt}\n\n{prompt}"
        
        resp = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=full_prompt,
            config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "response_mime_type": "application/json"
            }
        )
        
        text = resp.text.strip() if resp.text else ""
        text = text.replace("```json", "").replace("```", "").strip()
        
        print(f"[GEMINI_LLM] Response received ({len(text)} chars)")
        return text
        
    except Exception as e:
        print(f"[ERROR] call_gemini_llm failed: {e}", file=sys.stderr)
        raise


def fix_common_json_issues(json_str: str) -> str:
    """Perbaiki masalah sintaks JSON yang umum."""
    s = json_str
    # Hapus trailing commas
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    # Hapus karakter non-printable kecuali newlines, returns, tabs
    s = ''.join(ch for ch in s if ord(ch) >= 32 or ch in '\n\r\t')
    return s

def extract_json_from_text(text: str) -> Dict[str, Any]:
    """Ekstrak dan parse JSON dari response text LLM."""
    s = safe_strip(text)
    if not s:
        return validate_and_normalize_result({})
    
    # Coba parsing JSON biasa dulu
    try:
        parsed = json.loads(s)
        return validate_and_normalize_result(parsed)
    except Exception:
        pass
    
    # Coba cari JSON object dalam teks
    start = -1
    brace_count = 0
    
    for i, ch in enumerate(s):
        if ch == "{":
            if start == -1:
                start = i
            brace_count += 1
        elif ch == "}":
            brace_count -= 1
            if brace_count == 0 and start != -1:
                candidate = s[start:i+1]
                try:
                    parsed = json.loads(candidate)
                    return validate_and_normalize_result(parsed)
                except Exception:
                    # Coba perbaiki masalah umum
                    candidate_fixed = fix_common_json_issues(candidate)
                    try:
                        parsed = json.loads(candidate_fixed)
                        return validate_and_normalize_result(parsed)
                    except Exception:
                        pass
    
    # Fallback: kembalikan response HOAX default
    print("[PERINGATAN] Tidak dapat mem-parse JSON dari LLM. Mengembalikan response HOAX default.")
    return validate_and_normalize_result({})

def validate_and_normalize_result(result: dict) -> dict:
    """Validasi dan normalisasi result dari LLM ke format yang konsisten."""
    result = result or {}
    
    # Normalisasi label - tambah support untuk PARTIALLY_VALID
    if "label" not in result:
        result["label"] = "HOAX"
    
    label = safe_strip(result.get("label", "")).upper()
    if label not in ["VALID", "HOAX", "PARTIALLY_VALID"]:
        label_mapping = {
            "TRUE": "VALID", "BENAR": "VALID",
            "FALSE": "HOAX", "SALAH": "HOAX",
            "PARTIAL": "PARTIALLY_VALID", "SEBAGIAN": "PARTIALLY_VALID",
            "PARTIALLY_TRUE": "PARTIALLY_VALID", "SEBAGIAN_BENAR": "PARTIALLY_VALID",
            "CONDITIONAL": "PARTIALLY_VALID", "KONDISIONAL": "PARTIALLY_VALID",
            "CONTEXT_DEPENDENT": "PARTIALLY_VALID",
            "UNCERTAIN": "PARTIALLY_VALID", "TIDAK_PASTI": "PARTIALLY_VALID"
        }
        result["label"] = label_mapping.get(label, "HOAX")
    else:
        result["label"] = label
    
    # Normalisasi confidence
    try:
        confidence = float(result.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
    except Exception:
        confidence = 0.0
    result["confidence"] = confidence
    
    # Set defaults
    result.setdefault("analysis", "")
    result.setdefault("evidence", [])
    result.setdefault("summary", "")
    result.setdefault("references", [])
    result.setdefault("conditions", "")  # Untuk PARTIALLY_VALID
    
    return result

def decide_final_label_and_confidence(parsed: Dict[str, Any], neighbors: List[Dict[str, Any]], 
                                    combined_confidence: float) -> Dict[str, Any]:
    """Tentukan label dan confidence akhir berdasarkan output LLM dan heuristics."""
    llm_label = parsed.get("label", "").upper()
    llm_confidence = float(parsed.get("confidence", 0.0))
    
    # Logika keputusan akhir - include PARTIALLY_VALID
    if llm_label in ["VALID", "HOAX", "PARTIALLY_VALID"] and llm_confidence >= LLM_CONF_THRESHOLD:
        final_label = llm_label
        final_confidence = llm_confidence
        decision_reason = f"Menggunakan vonis LLM (label={llm_label}, confidence={llm_confidence:.2f})"
    else:
        # Untuk fallback, gunakan combined confidence dengan threshold yang berbeda
        if combined_confidence >= 0.7:
            final_label = "VALID"
        elif combined_confidence >= 0.4:
            final_label = "PARTIALLY_VALID"
        else:
            final_label = "HOAX"
        
        final_confidence = combined_confidence
        decision_reason = f"Menggunakan fallback retriever+LLM gabungan (combined_confidence={combined_confidence:.2f})"
    
    return {
        "final_label": final_label,
        "final_confidence": float(final_confidence),
        "decision_reason": decision_reason
    }
def build_frontend_payload(claim: str, parsed: Dict[str, Any], neighbors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Buat payload untuk frontend dari parsed result dan neighbors."""
    # Map neighbors berdasarkan ID untuk referensi
    neighbor_map = {n.get("safe_id") or str(n.get("doc_id")): n for n in neighbors}
    
    # Proses evidence list
    evidence_list = parsed.get("evidence", []) or []
    references = []
    frontend_evidence = []
    
    for ev in evidence_list:
        safe_id = ev.get("safe_id") or ev.get("id") or ev.get("doc_id") or ""
        nb = neighbor_map.get(safe_id) or {}
        
        # Buat evidence item
        doi_val = safe_strip(ev.get("doi", "") or nb.get("doi", "") or "")
        url_val = ""
        if doi_val:
            doi_normalized = re.sub(r'^(urn:doi:|doi:)\s*', '', doi_val, flags=re.I)
            url_val = f"https://doi.org/{doi_normalized}"
        else:
            url_val = ""
        
        # Preferensi konten ringkasan dari LLM jika tersedia
        ev_summary = safe_strip(ev.get("summary") or "")
        ev_snippet = safe_strip(ev.get("snippet") or ev.get("text") or "")
        base_snippet = ev_summary or ev_snippet or safe_strip(nb.get("text", ""))

        # Judul untuk evidence / sources
        title_val = safe_strip(ev.get("title") or nb.get("title", "") or safe_id)

        evidence_item = {
            "safe_id": safe_id,
            "title": title_val,
            "snippet": base_snippet[:400],
            "source_snippet": safe_strip(nb.get("_text_translated") or nb.get("text", ""))[:400],
            "doi": doi_val,
            "url": url_val,
            "relevance_score": safe_float(
                ev.get("relevance_score", ev.get("relevance", nb.get("relevance_score", 0.0))),
                default=nb.get("relevance_score", 0.0) or 0.0,
            ),
        }
        frontend_evidence.append(evidence_item)
        
        # Buat reference
        ref_entry = {
            "safe_id": safe_id,
            "doi": doi_val,
            "url": url_val,
            "source_type": "journal" if doi_val else "other",
            "relevance": evidence_item["relevance_score"]
        }
        
        # Tambahkan ke references jika tidak duplikat
        if not any(r.get("safe_id") == ref_entry["safe_id"] and r.get("url") == ref_entry["url"] 
                  for r in references):
            references.append(ref_entry)
    
    # Fallback: gunakan top neighbors sebagai evidence jika kosong
    if not frontend_evidence:
        for n in neighbors[:6]:
            safe_id = n.get("safe_id") or n.get("doc_id") or ""
            doi_val = safe_strip(n.get("doi", "") or "")
            url_val = f"https://doi.org/{doi_val}" if doi_val else ""
            
            frontend_evidence.append({
                "safe_id": safe_id,
                "snippet": safe_strip(n.get("_text_translated") or n.get("text", ""))[:400],
                "source_snippet": safe_strip(n.get("_text_translated") or n.get("text", ""))[:400],
                "doi": doi_val,
                "url": url_val,
                "relevance_score": float(n.get("relevance_score", 0.0))
            })
    
    return {
        "claim": claim,
        "label": parsed.get("label", "HOAX"),
        "confidence": float(parsed.get("confidence", 0.0)),
        "summary": parsed.get("summary", "") or parsed.get("analysis", "") or "",
        "conclusion": parsed.get("conclusion", ""),
        "evidence": frontend_evidence,
        "references": references,
        "metadata": parsed.get("_meta", {})
    }


# Main verification function
def verify_claim_local(claim: str, k: int = 5, dry_run: bool = False, 
                      enable_expansion: bool = True, min_relevance: float = 0.25,
                      force_dynamic_fetch: bool = False, 
                      debug_retrieval: bool = False,
                      use_cache: bool = True) -> Dict[str, Any]:
    """
    MODIFIED: Database-first verification flow with caching.
    
    Flow:
    1. Cek cache terlebih dahulu
    2. Cek database lokal
    3. Jika tidak ada hasil yang cukup relevan, baru fetch dari API
    4. Verifikasi dengan LLM
    5. Simpan hasil ke cache
    """
    claim = safe_strip(claim)
    if not claim:
        raise ValueError("Klaim kosong.")
    
    # STEP 0: CHECK CACHE
    if use_cache and CACHE_ENABLED and not dry_run:
        cached_result = cache.get_cached_verification(claim)
        if cached_result:
            print("[0/6] CACHE HIT! Returning cached verification result.", file=sys.stderr)
            return cached_result

    if not db_available:
        print("[WARNING] Database not available", file=sys.stderr)

    try:
        # STEP 1: QUERY DATABASE LOKAL DULU
        print("[1/6] Mengambil dari database lokal...", file=sys.stderr)
        
        if enable_expansion:
            neighbors = retrieve_with_expansion_bilingual(claim, k=k, expand_queries=True, debug=debug_retrieval)
        else:
            emb = embed_texts_gemini([claim])[0]
            neighbors = retrieve_neighbors_from_db(emb, k=k, debug_print=debug_retrieval)
            neighbors = filter_by_relevance(claim, neighbors, min_relevance=min_relevance, debug=debug_retrieval)

        
        # STEP 2: CEK KUALITAS HASIL DATABASE
        MIN_DOCS_REQUIRED = 5  # Minimal 5 dokumen untuk skip API fetch
        
        db_quality_sufficient = (
            len(neighbors) >= MIN_DOCS_REQUIRED and 
            not needs_dynamic_fetch(
                neighbors, claim, 
                min_relevance_mean=min_relevance, 
                min_retriever_mean=0.25, 
                debug=debug_retrieval
            )
        )
        
        if db_quality_sufficient:
            print(f"[2/6] Database lokal memiliki {len(neighbors)} dokumen relevan. Skip API fetch.", file=sys.stderr)
        else:
            # Log alasan fetch
            if len(neighbors) < MIN_DOCS_REQUIRED:
                print(f"[2/6] Hanya {len(neighbors)} dokumen lokal (min: {MIN_DOCS_REQUIRED}). Fetching dari API eksternal...", file=sys.stderr)
            else:
                print("[2/6] Kualitas dokumen lokal tidak cukup. Fetching dari API eksternal...", file=sys.stderr)
            
            try:
                did_update, dynamic_selected = dynamic_fetch_and_update(claim, max_fetch_results=30, top_k_select=12)
                
                if did_update:
                    print("[DYNAMIC_FETCH] Data baru di-ingest. Retry retrieval...", file=sys.stderr)
                    time.sleep(2.0)
                    
                    # Retry retrieval setelah ingest
                    if enable_expansion:
                        neighbors = retrieve_with_expansion_bilingual(claim, k=k, expand_queries=True, debug=debug_retrieval)
                    else:
                        emb = embed_texts_gemini([claim])[0]
                        neighbors = retrieve_neighbors_from_db(emb, k=k*2, debug_print=debug_retrieval)
                        neighbors = filter_by_relevance(claim, neighbors, min_relevance=min_relevance, debug=debug_retrieval)
                    
                    # Fallback ke dynamic_selected jika masih tidak cukup
                    if not neighbors and dynamic_selected:
                        print("[DYNAMIC_FETCH] Menggunakan fetched items sebagai fallback.", file=sys.stderr)
                        neighbors = convert_dynamic_to_neighbors(dynamic_selected, claim)
                        neighbors = filter_by_relevance(claim, neighbors, min_relevance=min_relevance, debug=debug_retrieval)
                        
            except Exception as e:
                print(f"[ERROR] Dynamic fetch gagal: {e}", file=sys.stderr)

        # Handle kasus tidak ada neighbors
        if not neighbors:
            frontend = {
                "claim": claim,
                "label": "inconclusive",
                "confidence": 0.0,
                "summary": "Tidak ditemukan dokumen relevan untuk klaim ini setelah pencarian dan fetch otomatis.",
                "conclusion": "Tidak ada bukti yang ditemukan.",
                "evidence": [],
                "references": [],
                "metadata": {}
            }
            print("\n[JSON_OUTPUT]")
            print(json.dumps(frontend, ensure_ascii=False, indent=2))
            return {"_frontend_payload": frontend}

        # Menerjemahkan snippets untuk LLM
        neighbors = translate_snippets_if_needed(neighbors, target_lang="Indonesian")
        
        #  Hanya gunakan paper akademik, buang berita/blog
        print(f"[2.5/6] Filtering {len(neighbors)} neighbors untuk paper akademik...", file=sys.stderr)
        neighbors_before_filter = len(neighbors)
        neighbors = filter_academic_neighbors(neighbors)
        neighbors_after_filter = len(neighbors)
        print(f"[FILTER] Retained {neighbors_after_filter}/{neighbors_before_filter} academic papers", file=sys.stderr)

        if dry_run:
            print("\n=== DRY RUN: Neighbors ===", file=sys.stderr)
            for i, n in enumerate(neighbors, 1):
                relevance = n.get('relevance_score', 0)
                is_academic = n.get('_is_academic', False)
                print(f"\n[{i}] {n.get('safe_id')} (relevansi: {relevance:.3f}, academic: {is_academic})", file=sys.stderr)
                print(f"DOI: {n.get('doi', 'N/A')}", file=sys.stderr)
                print(f"Text: {n.get('_text_translated', n.get('text',''))[:400]}...", file=sys.stderr)
            return {"dry_run_neighbors": neighbors}

        print(f"[3/6] Membuat prompt dengan {len(neighbors)} academic neighbors...", file=sys.stderr)
        prompt = build_prompt(claim, neighbors, max_context_chars=3500)

        print("[4/6] Memanggil DeepSeek untuk verifikasi...", file=sys.stderr)
        try:
            raw_response = call_deepseek(prompt, temperature=0.1, max_tokens=2500)
        except Exception as e:
            print(f"[ERROR] call_deepseek gagal: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            raw_response = ""

        print("[5/6] Parsing hasil dari LLM...", file=sys.stderr)
        parsed = extract_json_from_text(raw_response) if raw_response else validate_and_normalize_result({})

        print("[6/6] Memperkaya dengan metadata dan membuat frontend payload...", file=sys.stderr)
        
        # Menghitung metrics
        similarity_scores = [distance_to_similarity(n.get("distance")) for n in neighbors if n.get("distance") is not None]
        relevance_scores = [n.get("relevance_score", 0.0) for n in neighbors]
        
        retriever_mean = float(sum(similarity_scores) / len(similarity_scores)) if similarity_scores else 0.0
        relevance_mean = float(sum(relevance_scores) / len(relevance_scores)) if relevance_scores else 0.0
        llm_confidence = float(parsed.get("confidence") or 0.0)
        
        combined_confidence = (0.5 * llm_confidence + 0.3 * relevance_mean + 0.2 * retriever_mean)

        # Menambahkan metadata
        parsed["_meta"] = {
            "neighbors_count": len(neighbors),
            "neighbors_with_doi": len([n for n in neighbors if n.get("doi")]),
            "retriever_mean_similarity": retriever_mean,
            "relevance_mean": relevance_mean,
            "combined_confidence": combined_confidence,
            "prompt_len_chars": len(prompt),
            "query_expansion_used": enable_expansion,
            "raw_llm_preview": safe_strip(raw_response)[:500] if raw_response else ""
        }

        # Logika keputusan akhir
        final_decision = decide_final_label_and_confidence(parsed, neighbors, combined_confidence)
        parsed.update(final_decision)
        
        # Memaksa label akhir (hanya VALID/HOAX)
        parsed["label"] = parsed["final_label"]
        parsed["confidence"] = parsed["final_confidence"]

        # Buat frontend payload
        frontend = build_frontend_payload(claim, parsed, neighbors)

        # Menyimpan hasil verifikasi agar bisa di-ingest oleh backend / pipeline lain
        try:
            payload_to_save = {
                "timestamp": int(time.time()),
                "claim": frontend.get("claim"),
                "frontend": frontend,
                "_parsed_meta": parsed.get("_meta", {}),
            }
            save_verification_result(payload_to_save)
        except Exception as e:
            print(f"[SAVE_VERIF] Warning: gagal menyimpan payload: {e}", file=sys.stderr)

        # Print JSON output dan return
        print("\n[JSON_OUTPUT]")
        print(json.dumps(frontend, ensure_ascii=False, indent=2))
        
        # Save to cache
        result = {"_frontend_payload": frontend}
        if use_cache and CACHE_ENABLED:
            try:
                cache.cache_verification(claim, result)
                print("[CACHE]  Result cached for future use", file=sys.stderr)
            except Exception as cache_err:
                print(f"[CACHE] Warning: Failed to cache: {cache_err}", file=sys.stderr)
        
        return result

    except Exception as e:
        error_msg = f"Exception dalam verify_claim_local: {str(e)}"
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        
        # Return error response instead of raising
        error_frontend = {
            "claim": claim,
            "label": "inconclusive",
            "confidence": 0.0,
            "summary": f"Error dalam verifikasi: {str(e)}",
            "conclusion": "Terjadi kesalahan teknis saat memproses klaim.",
            "evidence": [],
            "references": [],
            "metadata": {"error": error_msg}
        }
        print("\n[JSON_OUTPUT]")
        print(json.dumps(error_frontend, ensure_ascii=False, indent=2))
        return {"_frontend_payload": error_frontend}


# CLI main function
def main():
    """CLI entry point untuk menjalankan fact-checking system."""
    parser = argparse.ArgumentParser(
        description="Improved Prompt & Verify - Healthify RAG dengan bilingual retrieval dan LLM-based pattern expansion"
    )
    parser.add_argument("--claim", "-c", type=str,
                       help="Klaim yang akan diverifikasi (kosong untuk mode interaktif)")
    parser.add_argument("--k", "-k", type=int, default=5,
                       help="Jumlah neighbor yang diambil (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Tampilkan neighbors saja tanpa LLM call")
    parser.add_argument("--no-expansion", action="store_true",
                       help="Disable query expansion (gunakan single query)")
    parser.add_argument("--min-relevance", type=float, default=0.25,
                       help="Minimum relevance score untuk filter (default: 0.25)")
    parser.add_argument("--save-json", type=str, default=None,
                       help="Simpan hasil ke file JSON")
    parser.add_argument("--force-dynamic-fetch", action="store_true",
                       help="Memaksa dynamic fetch meskipun retrieval awal tampak cukup")
    parser.add_argument("--enable-prefetch", action="store_true",
                       help="Jalankan pre-fetch dengan query expansion sebelum verifikasi")
    parser.add_argument("--debug-retrieval", action="store_true",
                       help="Tampilkan debug info untuk retrieval")
    
    args = parser.parse_args()

    claim = args.claim
    if not claim:
        print("Masukkan klaim (akhiri dengan ENTER): ", file=sys.stderr)
        claim = input("> ").strip()

    if not claim:
        print("Error: Klaim tidak boleh kosong", file=sys.stderr)
        sys.exit(1)

    if args.enable_prefetch:
        queries = generate_bilingual_queries(claim, langs=["English"])
        print(f"[MAIN] Generated queries untuk fetch: {queries}", file=sys.stderr)

        for q in queries:
            try:
                print(f"[MAIN] Fetching untuk expanded query: {q}", file=sys.stderr)
                fetch_all_sources(q, pubmed_max=5, crossref_rows=5, semantic_limit=5, delay_between_sources=0.3)
            except Exception as e:
                print(f"[MAIN] fetch_all_sources gagal untuk '{q}': {e}", file=sys.stderr)

    try:
        result = verify_claim_local(
            claim,
            k=args.k,
            dry_run=args.dry_run,
            enable_expansion=not args.no_expansion,
            min_relevance=args.min_relevance,
            force_dynamic_fetch=args.force_dynamic_fetch,
            debug_retrieval=args.debug_retrieval
        )
    except Exception as e:
        print(f"\nGagal verifikasi klaim: {str(e)}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # Print ringkasan yang user-friendly
    print("\n" + "="*60, file=sys.stderr)
    print("HASIL VERIFIKASI (ringkasan untuk pengguna)", file=sys.stderr)
    print("="*60, file=sys.stderr)

    frontend = result.get("_frontend_payload") if isinstance(result, dict) else None
    if frontend:
        print(f"\nKLAIM: {frontend.get('claim')}", file=sys.stderr)
        print(f"\nLABEL: {frontend.get('label')}", file=sys.stderr)
        print(f"CONFIDENCE: {frontend.get('confidence'):.2%}", file=sys.stderr)
        print(f"\nRINGKASAN:\n{frontend.get('summary','')}", file=sys.stderr)
        print(f"\nKESIMPULAN:\n{frontend.get('conclusion','')}", file=sys.stderr)
        
        metadata = frontend.get("metadata", {})
        if metadata:
            print(f"\nMETADATA:", file=sys.stderr)
            print(f"   - Neighbors yang diambil: {metadata.get('neighbors_count', 0)}", file=sys.stderr)
            print(f"   - Neighbors dengan DOI: {metadata.get('neighbors_with_doi', 0)}", file=sys.stderr)
            print(f"   - Mean relevance score: {metadata.get('relevance_mean', 0):.3f}", file=sys.stderr)
            print(f"   - Mean similarity score: {metadata.get('retriever_mean_similarity', 0):.3f}", file=sys.stderr)
            print(f"   - Combined confidence: {metadata.get('combined_confidence', 0):.2%}", file=sys.stderr)

    if args.save_json and frontend:
        with open(args.save_json, "w", encoding="utf-8") as fo:
            json.dump(frontend, fo, ensure_ascii=False, indent=2)
        print(f"\nHasil disimpan ke: {args.save_json}", file=sys.stderr)

    print("\n" + "="*60, file=sys.stderr)
    print(" Selesai.", file=sys.stderr)
    print("="*60, file=sys.stderr)


if __name__ == "__main__":
    main()