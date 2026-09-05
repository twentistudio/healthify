import logging
import hashlib
import re
import os
from google import genai
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
import django
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from .permissions import IsAdminOrReadOnly
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction, models, connection
from django.db.models import Q
from django.http import Http404
from django.conf import settings
from django.core.cache import cache
import json
from .models import Claim, VerificationResult, Source, ClaimSource, Dispute, JournalArticle
from .serializers import (
    ClaimCreateSerializer, 
    ClaimDetailSerializer, 
    DisputeCreateSerializer, 
    DisputeDetailSerializer,
    DisputeAdminActionSerializer,
    JournalArticleSerializer,
    JournalArticleCreateSerializer
)
from .text_normalization import (
    ClaimSimilarityMatcher, 
    calculate_text_similarity,
    normalize_claim_text
)
from . import text_normalization as text_norm
from .ai_adapter import VERIFICATION_LOGIC_VERSION, call_ai_verify
from .email_service import email_service

logger = logging.getLogger(__name__)

_gemini_client = None

# Utility Functions 
def get_gemini_client():
    """Get Gemini client, returns None if API key not available."""
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY not found - Gemini features disabled")
            return None
        try:
            _gemini_client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            return None
    return _gemini_client

@api_view(['GET'])
@permission_classes([AllowAny])
def health_check(request):
    """Health check endpoint untuk Railway"""
    try:
        connection.ensure_connection()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return Response({
        'status': 'healthy',
        'django_version': django.get_version(),
        'database': db_status,
        'timestamp': timezone.now().isoformat()
    }, status=200)
    
def normalize_claim_text(text: str) -> str:
    """
    Normalisasi teks klaim untuk konsistensi - IMPROVED VERSION.
    
    Proses normalisasi:
    1. Lowercase
    2. Remove extra whitespace
    3. Remove punctuation (kecuali yang bermakna medis)
    4. Standardize medical terms
    5. Remove common stop words (optional)
    """
    if not text:
        return ""
    
    # 1. Lowercase
    normalized = text.lower().strip()
    
    # 2. Replace multiple spaces/tabs/newlines dengan single space
    normalized = re.sub(r'\s+', ' ', normalized)
    
    # 3. Remove punctuation KECUALI yang bermakna 
    normalized = re.sub(r'[.,!?;:\"\'\(\)\[\]\{\}_]', '', normalized)
    normalized = re.sub(r'-+', ' ', normalized) 
    
    # 4. Standardisasi variasi ejaan medis umum
    medical_variations = {
        r'\bkanker paru paru\b': 'kanker paru',
        r'\bkanker paruparu\b': 'kanker paru',
        r'\bparu paru\b': 'paru',
        r'\bdiabetes mellitus\b': 'diabetes',
        r'\bdiabetes melitus\b': 'diabetes',
        r'\btekanan darah tinggi\b': 'hipertensi',
        r'\bserangan jantung\b': 'infark miokard',
        r'\bstroke\b': 'stroke',
        r'\bcovid 19\b': 'covid19',
        r'\bcovid-19\b': 'covid19',
    }
    
    for pattern, replacement in medical_variations.items():
        normalized = re.sub(pattern, replacement, normalized)
    
    # 5. Remove extra spaces lagi setelah replacements
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized

def generate_claim_hash(text: str) -> str:
    """
    Generate hash unik untuk claim text dengan normalisasi yang lebih baik.
    
    Hash ini digunakan untuk:
    - Deteksi duplikasi claim
    - Cache lookup
    """
    normalized = normalize_claim_text(text)
    return hashlib.sha256(normalized.encode()).hexdigest()

def check_cached_result(claim_text: str):
    """
    Mengecek apakah claim sudah pernah diverifikasi sebelumnya.
    
    Returns:
        tuple: (is_cached, claim_object, verification_result)
    """
    try:
        # Normalisasi teks klaim menggunakan text_normalization (konsisten dengan model Claim)
        normalized = text_norm.normalize_claim_text(claim_text)

        # Ambil semua klaim dengan teks ternormalisasi yang sama dan status DONE
        # beserta VerificationResult-nya (jika ada).
        claims_qs = (
            Claim.objects
            .filter(text_normalized=normalized, status=Claim.STATUS_DONE)
            .select_related('verification_result')
        )

        if not claims_qs.exists():
            logger.info("[CACHE MISS] Claim tidak ditemukan di cache.")
            return False, None, None

        # Hanya hasil dari versi logika saat ini yang boleh disajikan. Hasil
        # lama dianggap CACHE MISS agar diverifikasi ulang oleh mesin terkini.
        prioritized_claims = list(
            claims_qs
            .filter(verification_result__isnull=False,
                    verification_result__logic_version=VERIFICATION_LOGIC_VERSION)
            .order_by('-verification_result__updated_at', '-updated_at')
        )

        stale = claims_qs.filter(verification_result__isnull=False).exclude(
            verification_result__logic_version=VERIFICATION_LOGIC_VERSION
        ).count()
        if stale:
            logger.info(
                "[CACHE] %d hasil lama (versi != %s) diabaikan; klaim akan diverifikasi ulang.",
                stale, VERIFICATION_LOGIC_VERSION,
            )

        for claim in prioritized_claims:
            vr = getattr(claim, 'verification_result', None)
            if vr and vr.label != VerificationResult.LABEL_UNVERIFIED:
                logger.info(
                    f"[CACHE HIT] Using non-unverified result for claim ID: {claim.id} "
                    f"(label={vr.label}, updated_at={vr.updated_at})"
                )
                return True, claim, vr

        # 2) Jika tidak ada yang non-unverified, tapi ada VerificationResult, gunakan yang terbaru.
        if prioritized_claims:
            claim = prioritized_claims[0]
            vr = claim.verification_result
            logger.info(
                f"[CACHE HIT] Using latest available result for claim ID: {claim.id} "
                f"(label={vr.label}, updated_at={vr.updated_at})"
            )
            return True, claim, vr

        # 3) Tidak ada VerificationResult sama sekali untuk klaim-klaim ini
        logger.info("[CACHE MISS] Claim ditemukan tetapi belum memiliki hasil verifikasi.")
        return False, None, None

    except Exception as e:
        logger.error(f"[CACHE ERROR] Terjadi kesalahan saat mengecek cache: {str(e)}", exc_info=True)
        return False, None, None

def find_similar_claims(claim_text: str, threshold: float = 0.85) -> list:
    """
    Cari claim yang mirip berdasarkan similarity score.
    Berguna untuk mendeteksi claim yang semantically similar tapi tidak exact match.
    
    Args:
        claim_text: Text claim yang dicari
        threshold: Minimum similarity score (0-1)
    
    Returns:
        list: List of similar claims
    """
    from difflib import SequenceMatcher
    
    normalized = normalize_claim_text(claim_text)
    
    # Get recent claims untuk comparison
    recent_claims = Claim.objects.filter(
        status=Claim.STATUS_DONE
    ).order_by('-created_at')[:100]
    
    similar_claims = []
    
    for claim in recent_claims:
        if not claim.text_normalized:
            continue
            
        # Calculate similarity ratio
        similarity = SequenceMatcher(
            None, 
            normalized, 
            claim.text_normalized
        ).ratio()
        
        if similarity >= threshold:
            similar_claims.append({
                'claim': claim,
                'similarity': similarity
            })
    
    # Sort by similarity descending
    similar_claims.sort(key=lambda x: x['similarity'], reverse=True)
    
    return similar_claims

def translate_with_cache(text: str, target_lang: str, cache_prefix: str = "translate") -> str:
    """Wrapper translate dengan cache berbasis Django cache framework.

    Cache key dibangun dari prefix + target_lang + hash teks asli,
    sehingga teks yang sama dan bahasa target yang sama tidak diterjemahkan ulang.
    """
    if not text:
        return text

    # Gunakan SHA256 agar key tetap pendek tapi unik
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache_key = f"{cache_prefix}:{target_lang}:{text_hash}"

    cached = cache.get(cache_key)
    if cached:
        return cached

    translated = translate_text(text, target_lang)

    # Simpan di cache, misal 24 jam (86400 detik)
    try:
        cache.set(cache_key, translated, timeout=86400)
    except Exception as e:
        logger.warning(f"[TRANSLATE_CACHE] Failed to set cache: {e}")

    return translated

# Claim Views
@api_view(['POST'])
def translate_verification_result(request):
    """
    POST /api/translate/
    
    Translate verification result (label + summary + claim_text) ke bahasa target.
    
    Body:
    {
        "label": "FAKTA",
        "summary": "Merokok...",
        "claim_text": "Merokok itu sehat",
        "target_language": "en"  // or "id"
    }
    """
    try:
        label = request.data.get('label', '')
        summary = request.data.get('summary', '')
        claim_text = request.data.get('claim_text', '')
        target_lang = request.data.get('target_language', 'en')
        
        if not label and not summary and not claim_text:
            return Response({
                'error': 'Label, summary, or claim_text required'
            }, status=400)
        
        # Translate label
        translated_label = translate_label(label, target_lang) if label else ''
        
        # Translate summary & claim text (dengan cache)
        translated_summary = translate_with_cache(summary, target_lang, cache_prefix="translate:summary") if summary else ""
        translated_claim_text = translate_with_cache(claim_text, target_lang, cache_prefix="translate:claim") if claim_text else ""
        
        return Response({
            'translated_label': translated_label,
            'translated_summary': translated_summary,
            'translated_claim_text': translated_claim_text,
            'original_label': label,
            'original_summary': summary,
            'original_claim_text': claim_text,
            'target_language': target_lang
        })
        
    except Exception as e:
        logger.error(f"[TRANSLATE] Error: {e}")
        return Response({
            'error': 'Translation failed',
            'detail': str(e)
        }, status=500)

def translate_label(label: str, target_lang: str) -> str:
    """Translate label dengan mapping sederhana."""
    label_lower = label.lower().strip()
    
    if target_lang == 'en':
        mapping = {
            'fakta': 'FACT',
            'valid': 'VALID',
            'hoax': 'HOAX',
            'tidak pasti': 'UNCERTAIN',
            'uncertain': 'UNCERTAIN',
            'tidak terverifikasi': 'UNVERIFIED',
            'unverified': 'UNVERIFIED'
        }
    else:  # Indonesian
        mapping = {
            'fact': 'FAKTA',
            'valid': 'FAKTA',
            'hoax': 'HOAX',
            'uncertain': 'TIDAK PASTI',
            'unverified': 'TIDAK TERVERIFIKASI'
        }
    
    return mapping.get(label_lower, label.upper())

def translate_text(text: str, target_lang: str) -> str:
    """Terjemahkan lewat penyedia LLM yang benar-benar tersedia.

    Urutan: Gemini (bila dikonfigurasi) -> rantai provider LLM (OpenAI dsb)
    -> teks asli.

    Sebelumnya terjemahan hanya lewat Gemini; tanpa kunci Gemini fitur ini
    diam-diam mengembalikan teks asli sehingga tombol ganti bahasa di frontend
    tampak tidak berfungsi.
    """
    if not text or len(text) < 10:
        return text

    from .intelligence.reasoning import llm

    # Gemini hanya dicoba bila memang termasuk provider aktif. Tanpa penjagaan
    # ini, deployment yang sudah full OpenAI tetap membayar satu round-trip
    # gagal ke Gemini pada setiap permintaan terjemahan.
    if llm.PROVIDER_GEMINI in llm.configured_providers() and get_gemini_client() is not None:
        translated = translate_text_gemini(text, target_lang)
        if translated and translated.strip() != text.strip():
            return translated

    return translate_text_llm(text, target_lang) or text


def translate_text_llm(text: str, target_lang: str) -> str:
    """Terjemahan memakai rantai provider LLM (OpenAI, dsb)."""
    from .intelligence.reasoning import llm

    lang_name = "English" if target_lang == 'en' else "Indonesian"
    prompt = f"""You are a professional medical translator.
Translate the following medical/health text into {lang_name}.

Requirements:
- Output **only** the translated text in {lang_name}.
- Do not add explanations, notes, alternative phrasings, or quotes.
- Keep the style and length similar to the original.
- Preserve medical terminology accuracy.

Text to translate:
{text}
"""
    try:
        result = llm.generate(prompt, temperature=0.0, max_tokens=1000)
    except Exception as e:
        logger.error(f"[TRANSLATE_LLM] Error: {e}")
        return ""
    return (result or "").strip()


def translate_text_gemini(text: str, target_lang: str) -> str:
    """Translate text menggunakan Gemini API (output hanya teks terjemahan).
    Returns original text if Gemini not available."""
    if not text or len(text) < 10:
        return text
    
    try:
        client = get_gemini_client()
        
        # If Gemini not available, return original text
        if client is None:
            logger.warning("[TRANSLATE] Gemini client not available, returning original text")
            return text
        
        lang_name = "English" if target_lang == 'en' else "Indonesian"
        
        prompt = f"""You are a professional medical translator.
Translate the following medical/health text into {lang_name}.

Requirements:
- Output **only** the translated text in {lang_name}.
- Do not add explanations, notes, alternative phrasings, or quotes.
- Keep the style and length similar to the original.
- Preserve medical terminology accuracy.

Text to translate:
{text}
"""
        
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={
                "temperature": 0.0,
                "max_output_tokens": 1000
            }
        )
        
        # Extract text
        if hasattr(resp, 'text'):
            translated = resp.text.strip()
        elif hasattr(resp, 'candidates') and resp.candidates:
            if hasattr(resp.candidates[0], 'content'):
                if hasattr(resp.candidates[0].content, 'parts'):
                    translated = resp.candidates[0].content.parts[0].text.strip()
                else:
                    translated = str(resp.candidates[0].content)
            else:
                translated = text
        else:
            translated = text
        
        return translated or text
        
    except Exception as e:
        logger.error(f"[TRANSLATE_GEMINI] Error: {e}")
        return text  # Fallback to original

class ClaimVerifyView(APIView):
    """Main endpoint untuk verifikasi klaim."""

    SIMILARITY_THRESHOLD = 0.90

    def post(self, request):
        """Terima klaim baru dan jalankan verifikasi AI (tanpa cache)."""
        logger.info(f"[VERIFY] Received request from {request.META.get('REMOTE_ADDR', 'unknown')}")

        serializer = ClaimCreateSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(f"[VERIFY] Invalid request data: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        claim_text = serializer.validated_data.get("text", "")
        logger.info(f"[VERIFY] Processing claim: '{claim_text[:80]}'...")

        # Cek apakah klaim ini sudah pernah diverifikasi (cache berbasis database)
        is_cached, cached_claim, cached_verification = check_cached_result(claim_text)
        if is_cached and cached_claim and cached_verification:
            logger.info(f"[VERIFY] Using cached verification result for existing claim {cached_claim.id}")
            data = ClaimDetailSerializer(cached_claim).data
            return Response(data, status=status.HTTP_200_OK)

        try:
            # Klaim yang sama pernah diverifikasi oleh mesin versi lama:
            # perbarui baris yang ada, jangan menumpuk duplikat teks yang sama.
            claim = self._find_stale_claim(claim_text) or self._create_new_claim(claim_text)
            self._process_verification(claim)

            claim.status = Claim.STATUS_DONE
            claim.save()

            logger.info(f"[VERIFY] Successfully processed claim {claim.id}")
            data = ClaimDetailSerializer(claim).data
            return Response(data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"[VERIFY] Verification failed: {e}", exc_info=True)
            return self._handle_verification_error(e, claim_text, request)

    # Helper: tangani kegagalan verifikasi
    def _handle_verification_error(self, exc: Exception, claim_text: str, request):
        """
        Kembalikan response error yang konsisten saat verifikasi gagal.

        Sebelumnya method ini dirujuk di `post()` tetapi tidak pernah
        didefinisikan, sehingga setiap kegagalan AI berubah menjadi
        AttributeError (HTTP 500 tanpa pesan yang berguna).
        """
        logger.error(
            f"[VERIFY] Gagal memverifikasi klaim '{claim_text[:80]}': {exc}",
            exc_info=True,
        )
        return Response(
            {
                'error': 'Verification failed',
                'detail': 'Sistem tidak dapat menyelesaikan verifikasi klaim saat ini. Silakan coba lagi.',
                'claim_text': claim_text,
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    def _find_stale_claim(self, claim_text: str):
        """Klaim dengan teks sama yang hasilnya dibuat mesin versi lama."""
        normalized = text_norm.normalize_claim_text(claim_text)
        claim = (
            Claim.objects
            .filter(text_normalized=normalized, verification_result__isnull=False)
            .exclude(verification_result__logic_version=VERIFICATION_LOGIC_VERSION)
            .order_by("-updated_at")
            .first()
        )
        if claim:
            logger.info(
                "[VERIFY] Memverifikasi ulang klaim %s (hasil lama versi %s)",
                claim.id, claim.verification_result.logic_version,
            )
        return claim

    # Helper: create new Claim
    def _create_new_claim(self, claim_text: str) -> Claim:
        normalized_text = normalize_claim_text(claim_text)
        text_hash = generate_claim_hash(claim_text)

        claim = Claim.objects.create(
            text=claim_text,
            text_normalized=normalized_text,
            text_hash=text_hash,
            status=Claim.STATUS_PROCESSING,
        )

        logger.info(
            f"[VERIFY] Created Claim ID: {claim.id} (hash: {text_hash[:16]}...)"
        )
        logger.debug(f"[VERIFY] Normalized: '{normalized_text}'")
        return claim

    # Helper: call AI and create VerificationResult
    def _process_verification(self, claim: Claim) -> VerificationResult:
        ai_result = call_ai_verify(claim.text)

        logger.info(f"[VERIFY] AI verification completed for claim {claim.id}")
        logger.debug(
            f"[VERIFY] AI result summary: {ai_result.get('summary', '')[:100]}..."
        )

        sources_data = ai_result.get("sources", [])
        confidence = ai_result.get("confidence")
        summary = ai_result.get("summary", "")
        label = ai_result.get("label", "unverified")

        valid_labels = ["valid", "hoax", "uncertain", "unverified"]
        if label not in valid_labels:
            logger.warning(
                f"[VERIFY] Invalid label '{label}' dari AI, fallback ke 'unverified'"
            )
            label = "unverified"

        verification, created = VerificationResult.objects.update_or_create(
            claim=claim,
            defaults={
                "label": label,
                "summary": summary,
                "confidence": confidence,
                "logic_version": VERIFICATION_LOGIC_VERSION,
            },
        )

        # Sumber lama berasal dari mesin versi sebelumnya dan tidak boleh
        # bercampur dengan hasil baru.
        if not created:
            ClaimSource.objects.filter(claim=claim).delete()

        logger.info(
            f"[VERIFY] Created VerificationResult ID: {verification.id} - "
            f"Label: {label}, "
            f"Confidence: {confidence if confidence is not None else 'N/A'}, "
            f"Sources: {len(sources_data)}",
        )

        if sources_data:
            self._process_sources(claim, sources_data)

        return verification

    def _process_sources(self, claim: Claim, sources_data):
        """Simpan dan kaitkan sumber AI ke ClaimSource/Source."""
        processed_count = 0

        for idx, source_data in enumerate(sources_data):
            try:
                source = self._create_or_get_source(source_data)

                if ClaimSource.objects.filter(claim=claim, source=source).exists():
                    logger.info(
                        f"[VERIFY] Duplicate ClaimSource skipped for claim {claim.id} "
                        f"and source {source.id}"
                    )
                else:
                    ClaimSource.objects.create(
                        claim=claim,
                        source=source,
                        relevance_score=source_data.get("relevance_score", 0.0),
                        excerpt=source_data.get("excerpt", ""),
                        rank=idx + 1,
                    )
                    processed_count += 1

            except Exception as e:
                logger.error(
                    f"[VERIFY] Error processing source for claim {claim.id}: {e}",
                    exc_info=True,
                )

        logger.info(
            f"[VERIFY] Linked {processed_count}/{len(sources_data)} sources "
            f"to claim {claim.id}"
        )

    def _create_or_get_source(self, source_data):
        """Buat atau ambil Source berdasarkan DOI/URL."""
        doi = (source_data.get("doi") or "").strip()
        url = (source_data.get("url") or "").strip()

        if doi:
            existing = Source.objects.filter(doi=doi).first()
            if existing:
                return existing

        if url:
            existing = Source.objects.filter(url=url).first()
            if existing:
                return existing

        source = Source.objects.create(
            title=(source_data.get("title") or "Unknown")[:500],
            doi=doi or None,
            url=url or None,
            authors=source_data.get("authors", ""),
            publisher=(source_data.get("publisher") or "")[:255],
            published_date=source_data.get("published_date"),
            source_type=source_data.get("source_type", "journal"),
            credibility_score=source_data.get("credibility_score", 0.5),
        )

        logger.debug(f"[VERIFY] Created new Source ID: {source.id}")
        return source

class ClaimDetailView(APIView):
    """
    GET endpoint untuk mendapatkan detail klaim berdasarkan ID.
    
    Returns:
        - 200: Claim detail dengan verification result
        - 404: Claim tidak ditemukan
        - 500: Server error
    """

    def get(self, request, claim_id):
        """Retrieve detailed information for a specific claim."""
        logger.info(f"[CLAIM_DETAIL] Fetching claim ID: {claim_id}")
        
        try:
            claim = self._get_claim_or_404(claim_id)
            serializer = ClaimDetailSerializer(claim)
            
            logger.info(f"[CLAIM_DETAIL] Successfully retrieved claim {claim_id}")
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Http404:
            logger.warning(f"[CLAIM_DETAIL] Claim {claim_id} not found")
            raise
            
        except Exception as e:
            logger.error(f"[CLAIM_DETAIL] Unexpected error for claim {claim_id}: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to fetch claim details',
                    'detail': 'An unexpected error occurred'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _get_claim_or_404(self, claim_id):
        """
        Get claim by ID or raise 404.
        
        Args:
            claim_id: The claim ID to fetch
            
        Returns:
            Claim object with prefetched relations
            
        Raises:
            Http404: If claim doesn't exist
        """
        return get_object_or_404(
            Claim.objects.select_related('verification_result')
                         .prefetch_related('sources'),
            id=claim_id
        )

class ClaimListView(APIView):
    """
    GET endpoint untuk list claims dengan pagination dan filtering.
    
    Query Parameters:
        - search (str): Search term untuk claim text
        - label (str): Filter by label (valid, hoax, uncertain, unverified)
        - page (int): Page number (default: 1)
        - per_page (int): Items per page (default: 50, max: 100)
    
    Returns:
        - 200: List of claims dengan pagination info
        - 400: Invalid parameters
        - 500: Server error
    """
    
    DEFAULT_PAGE = 1
    DEFAULT_PER_PAGE = 50
    MAX_PER_PAGE = 100
    
    # Valid filter labels
    VALID_LABELS = ['valid', 'hoax', 'uncertain', 'unverified']

    def get(self, request):
        """List all claims with filtering and pagination."""
        logger.info(f"[CLAIM_LIST] Request from {request.META.get('REMOTE_ADDR', 'unknown')}")
        
        try:
            # Parse and validate query parameters
            params = self._parse_query_params(request)
            
            # Build queryset with filters
            claims = self._build_queryset(params)
            
            # Get total count before pagination
            total = claims.count()
            
            # Apply pagination
            claims_page = self._paginate_queryset(claims, params)
            
            # Serialize claims data
            claims_data = self._serialize_claims(claims_page)
            
            # Build pagination metadata
            pagination = self._build_pagination_metadata(params, total)
            
            logger.info(
                f"[CLAIM_LIST] Returned {len(claims_data)} claims "
                f"(page {params['page']}/{pagination['total_pages']}, total {total})"
            )
            
            return Response(
                {
                    'claims': claims_data,
                    'pagination': pagination,
                    'filters': {
                        'search': params['search'],
                        'label': params['label']
                    }
                },
                status=status.HTTP_200_OK
            )
            
        except ValueError as e:
            logger.warning(f"[CLAIM_LIST] Invalid parameters: {str(e)}")
            return Response(
                {
                    'error': 'Invalid parameters',
                    'detail': str(e)
                },
                status=status.HTTP_400_BAD_REQUEST
            )
            
        except Exception as e:
            logger.error(f"[CLAIM_LIST] Unexpected error: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to fetch claims',
                    'detail': 'An unexpected error occurred'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _parse_query_params(self, request):
        """
        Parse and validate query parameters.
        
        Args:
            request: The HTTP request object
            
        Returns:
            dict: Validated parameters
            
        Raises:
            ValueError: If parameters are invalid
        """
        # Search term
        search = request.GET.get('search', '').strip()
        
        # Label filter
        label_filter = request.GET.get('label', '').strip().lower()
        if label_filter and label_filter not in ['all', ''] + self.VALID_LABELS:
            raise ValueError(
                f"Invalid label filter. Must be one of: {', '.join(self.VALID_LABELS)}"
            )
        
        # Pagination
        try:
            page = int(request.GET.get('page', self.DEFAULT_PAGE))
            if page < 1:
                raise ValueError("Page must be >= 1")
        except (ValueError, TypeError):
            raise ValueError("Invalid page number")
        
        try:
            per_page = int(request.GET.get('per_page', self.DEFAULT_PER_PAGE))
            if per_page < 1:
                raise ValueError("per_page must be >= 1")
            if per_page > self.MAX_PER_PAGE:
                per_page = self.MAX_PER_PAGE
        except (ValueError, TypeError):
            raise ValueError("Invalid per_page number")
        
        return {
            'search': search,
            'label': label_filter if label_filter not in ['all', ''] else None,
            'page': page,
            'per_page': per_page
        }
    
    def _build_queryset(self, params):
        """
        Build queryset dengan filters yang diterapkan.
        
        Args:
            params (dict): Validated query parameters
            
        Returns:
            QuerySet: Filtered claims queryset
        """
        # Base queryset dengan optimized prefetch
        claims = Claim.objects.select_related(
            'verification_result'
        ).prefetch_related(
            'sources'
        ).order_by('-created_at')
        
        # Apply search filter
        if params['search']:
            claims = claims.filter(
                Q(text__icontains=params['search']) |
                Q(text_normalized__icontains=params['search'])
            )
        
        # Apply label filter
        if params['label']:
            claims = claims.filter(verification_result__label=params['label'])
        
        return claims
    
    def _paginate_queryset(self, queryset, params):
        """
        Apply pagination to queryset.
        
        Args:
            queryset: The queryset to paginate
            params (dict): Contains page and per_page
            
        Returns:
            QuerySet: Paginated slice of queryset
        """
        start = (params['page'] - 1) * params['per_page']
        end = start + params['per_page']
        return queryset[start:end]
    
    def _serialize_claims(self, claims):
        """
        Convert claims to serialized data.
        
        Args:
            claims: Iterable of Claim objects
            
        Returns:
            list: List of claim dictionaries
        """
        claims_data = []
        
        for claim in claims:
            claim_dict = self._serialize_claim(claim)
            claims_data.append(claim_dict)
        
        return claims_data
    
    def _serialize_claim(self, claim):
        """
        Serialize single claim object.
        
        Args:
            claim: Claim object
            
        Returns:
            dict: Serialized claim data
        """
        claim_dict = {
            'id': claim.id,
            'text': claim.text,
            'status': claim.status,
            'created_at': claim.created_at.isoformat(),
            'updated_at': claim.updated_at.isoformat(),
        }
        
        # Add verification result if exists
        if hasattr(claim, 'verification_result'):
            verification = self._serialize_verification_result(claim.verification_result)
            claim_dict.update(verification)
        else:
            claim_dict.update(self._get_default_verification())
        
        return claim_dict
    
    def _serialize_verification_result(self, vr):
        """
        Serialize verification result.
        
        Args:
            vr: VerificationResult object
            
        Returns:
            dict: Serialized verification data
        """
        return {
            'label': vr.label,
            'label_display': vr.get_label_display(),
            'confidence': round(vr.confidence, 4) if vr.confidence is not None else None,
            'confidence_percent': vr.confidence_percent(),
            'summary': vr.summary,
            'verification_created_at': vr.created_at.isoformat(),
            'verification_updated_at': vr.updated_at.isoformat()
        }
    
    def _get_default_verification(self):
        """
        Get default verification data for claims without results.
        
        Returns:
            dict: Default verification data
        """
        return {
            'label': VerificationResult.LABEL_UNVERIFIED,
            'label_display': 'Tidak Terverifikasi',
            'confidence': None,
            'confidence_percent': None,
            'summary': None,
            'verification_created_at': None,
            'verification_updated_at': None
        }
    
    def _build_pagination_metadata(self, params, total):
        """
        Build pagination metadata.
        
        Args:
            params (dict): Query parameters with page and per_page
            total (int): Total number of items
            
        Returns:
            dict: Pagination metadata
        """
        total_pages = (total + params['per_page'] - 1) // params['per_page']
        
        return {
            'page': params['page'],
            'per_page': params['per_page'],
            'total': total,
            'total_pages': max(total_pages, 1),
            'has_next': params['page'] < total_pages,
            'has_previous': params['page'] > 1
        }

# Dispute Views
class DisputeCreateView(APIView):
    """POST endpoint untuk membuat dispute baru"""
    
    def post(self, request):
        logger.info(f"[DISPUTE CREATE] Received request from {request.META.get('REMOTE_ADDR', 'unknown')}")
        
        serializer = DisputeCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        validated_data = serializer.validated_data
        claim_id = validated_data.get('claim_id')
        claim_text = validated_data.get('claim_text', '')
        
        # Auto-link dispute ke claim jika ada
        claim = None
        
        if claim_id:
            # Explicit claim_id provided
            try:
                claim = Claim.objects.get(id=claim_id)
                logger.info(f"[DISPUTE CREATE] Using explicit claim_id: {claim_id}")
            except Claim.DoesNotExist:
                logger.warning(f"[DISPUTE CREATE] Claim {claim_id} not found, will create without link")
        
        elif claim_text:
            # Try to find matching claim by text similarity
            try:
                from .text_normalization import normalize_claim_text, calculate_text_similarity
                
                normalized_input = normalize_claim_text(claim_text)
                
                # Find all claims dengan similarity >= 0.85
                all_claims = Claim.objects.filter(status=Claim.STATUS_DONE).values_list('id', 'text')
                
                best_match = None
                best_similarity = 0.0
                
                for cid, ctext in all_claims:
                    similarity = calculate_text_similarity(claim_text, ctext)
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match = cid
                
                # AUTO-LINK jika similarity >= 0.80
                if best_match and best_similarity >= 0.80:
                    claim = Claim.objects.get(id=best_match)
                    logger.info(
                        f"[DISPUTE CREATE] Auto-linked to Claim {best_match} "
                        f"(similarity: {best_similarity:.2%})"
                    )
                else:
                    logger.warning(
                        f"[DISPUTE CREATE] No good match found "
                        f"(best: {best_similarity:.2%}, threshold: 0.80)"
                    )
            
            except Exception as e:
                logger.warning(f"[DISPUTE CREATE] Error matching claim: {e}")
        
        # Store original verification result SEBELUM update
        original_label = None
        original_confidence = None
        
        if claim and hasattr(claim, 'verification_result'):
            vr = claim.verification_result
            original_label = vr.label
            original_confidence = vr.confidence
            logger.info(
                f"[DISPUTE CREATE] Storing original verification: "
                f"label={original_label}, confidence={original_confidence}"
            )
        
        # Create dispute
        try:
            dispute = Dispute.objects.create(
                claim=claim,  # Bisa None jika tidak ada match
                claim_text=claim_text or (claim.text if claim else ''),
                reason=validated_data['reason'],
                reporter_name=validated_data.get('reporter_name', 'Anonymous'),
                reporter_email=validated_data.get('reporter_email', ''),
                supporting_doi=validated_data.get('supporting_doi', ''),
                supporting_url=validated_data.get('supporting_url', ''),
                supporting_file=validated_data.get('supporting_file'),
                original_label=original_label or '',
                original_confidence=original_confidence
            )
            
            logger.info(f"[DISPUTE CREATE] Created dispute ID: {dispute.id}")
            
            # Send admin notification
            try:
                email_service.notify_admin_new_dispute(dispute)
            except Exception as e:
                logger.error(f"[DISPUTE CREATE] Failed to send admin notification: {e}")
            
            return Response(
                {
                    'id': dispute.id,
                    'message': 'Dispute created successfully',
                    'claim_linked': claim is not None
                },
                status=status.HTTP_201_CREATED
            )
        
        except Exception as e:
            logger.error(f"[DISPUTE CREATE] Error creating dispute: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to create dispute'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
class DisputeListView(APIView):
    """GET endpoint untuk list dispute"""

    def get(self, request):
        logger.info("[DISPUTE_LIST] Fetching disputes list")

        try:
            disputes = Dispute.objects.select_related('claim').order_by('-created_at')[:50]
            
            dispute_list = []
            for dispute in disputes:
                dispute_list.append({
                    'id': dispute.id,
                    'claim_text': dispute.claim_text[:100],
                    'status': dispute.status,
                    'created_at': dispute.created_at.isoformat()
                })
            
            return Response({
                'disputes': dispute_list,
                'total': disputes.count()
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"[DISPUTE_LIST] Error: {str(e)}", exc_info=True)
            return Response({
                'error': 'Failed to fetch disputes'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DisputeDetailView(APIView):
    """GET endpoint untuk detail satu dispute"""

    def get(self, request, dispute_id):
        logger.info(f"[DISPUTE_DETAIL] Fetching dispute ID: {dispute_id}")

        try:
            dispute = get_object_or_404(Dispute, id=dispute_id)
            serializer = DisputeDetailSerializer(dispute)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"[DISPUTE_DETAIL] Error: {str(e)}", exc_info=True)
            return Response({
                'error': 'Failed to fetch dispute details'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
@api_view(['POST'])
def check_claim_duplicate(request):
    """
    Check if incoming claim is duplicate/similar to existing claims
    """
    text = request.data.get('text', '')
    
    if not text:
        return Response(
            {'error': 'Text is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Get all existing claims
    existing_claims = Claim.objects.values('id', 'text', 'text_normalized')
    existing_list = [
        (c['id'], c['text'], c['text_normalized']) 
        for c in existing_claims
    ]
    
    # Find similar claims
    matcher = ClaimSimilarityMatcher()
    result = matcher.find_duplicates(text, existing_list)
    
    return Response({
        'text': text,
        'normalized': normalize_claim_text(text),
        'is_duplicate': result['match_found'],
        'match_level': result['match_level'],
        'matched_claim_id': result['claim_id'],
        'similarity_score': round(result['similarity'], 3),
        'explanation': _get_explanation(result['similarity']),
        'all_matches': [
            {'id': m[0], 'similarity': round(m[1], 3)} 
            for m in result.get('all_matches', [])[:5]
        ]
    })

def _get_explanation(similarity: float) -> str:
    """Helper function untuk penjelasan"""
    if similarity >= 0.95:
        return "Sangat mirip (kemungkinan besar duplikat)"
    elif similarity >= 0.85:
        return "Mirip (kemungkinan variasi dari klaim yang sama)"
    elif similarity >= 0.75:
        return "Agak mirip (mungkin topik yang sama)"
    else:
        return "Tidak mirip"

# Admin Journal Management
class AdminJournalListView(APIView):
    """
        GET: List journals,
        POST: Create new Journal
    """
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get(self, request):
        search = request.GET.get('search', '')
        source = request.GET.get('source', '')
        page = int(request.GET.get('page', 1))
        per_page = int(request.GET.get('per_page', 20))

        journals = JournalArticle.objects.all()

        if search:
            journals = journals.filter(
                Q(title__icontains=search) |
                Q(abstract__icontains=search) |
                Q(keywords__icontains=search)
            )

        if source:
            journals = journals.filter(source_portal=source)
            
        journals = journals.order_by('-created_at')
        total = journals.count()

        start = (page-1) * per_page
        journals_page = journals[start:start + per_page]

        return Response({
            'journals': JournalArticleSerializer(journals_page, many=True).data,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page
            }
        })
    
    def post(self, request):
        """Create new journal article"""
        serializer = JournalArticleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        journal = JournalArticle.objects.create(
            **serializer.validated_data,
            created_by=request.user
        )
        
        # Auto-embed if abstract is provided
        if journal.abstract:
            try:
                embed_journal_article(journal)
            except Exception as e:
                logger.warning(f"Auto-embed failed for journal {journal.id}: {e}")
        
        return Response({
            'message': 'Journal created successfully',
            'journal': JournalArticleSerializer(journal).data
        }, status=201)


class AdminJournalEmbedView(APIView):
    """Embed journal articles ke vector database"""
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    
    def post(self, request):
        """Batch embed journals yang belum di-embed"""
        journal_ids = request.data.get('journal_ids', [])
        
        if journal_ids:
            journals = JournalArticle.objects.filter(id__in=journal_ids, is_embedded=False)
        else:
            journals = JournalArticle.objects.filter(is_embedded=False)[:50]
        
        embedded_count = 0
        for journal in journals:
            try:
                embed_journal_article(journal)
                embedded_count += 1
            except Exception as e:
                logger.error(f"Embed failed for journal {journal.id}: {e}")
        
        return Response({
            'message': f'Embedded {embedded_count} journals',
            'embedded_count': embedded_count
        })


def embed_journal_article(journal: JournalArticle):
    """Embed single journal article to vector database.

    Embedding dihasilkan lewat penyedia yang tersedia (OpenAI / Gemini /
    sentence-transformers) dengan dimensi yang cocok dengan kolom vektor yang
    sudah ada, sehingga tidak perlu migrasi tabel maupun re-embed korpus lama.

    Penyimpanan ke pgvector bersifat best-effort: tabel dan ekstensinya dibuat
    otomatis bila belum ada, dan kegagalannya tidak membatalkan embedding yang
    sudah tersimpan di kolom `JournalArticle.embedding` — retrieval tetap jalan
    dari sana maupun secara leksikal.
    """
    from .intelligence.retrieval.embeddings import embed_text, store_vector

    text = f"{journal.title}\n\n{journal.abstract}"
    embedding = embed_text(text)
    if not embedding:
        raise RuntimeError(
            "Tidak ada penyedia embedding yang tersedia. Set OPENAI_API_KEY "
            "(atau GEMINI_API_KEY), lalu ulangi."
        )

    journal.embedding = json.dumps(embedding)
    journal.is_embedded = True
    journal.save(update_fields=["embedding", "is_embedded", "updated_at"])

    stored = store_vector(
        doc_id=f"journal_{journal.id}",
        safe_id=journal.doi or f"journal_{journal.id}",
        source_file=f"admin_import_{journal.source_portal}",
        text=text,
        doi=journal.doi or "",
        embedding=embedding,
    )
    if not stored:
        logger.info(
            "[EMBED] Jurnal %s ter-embed di database Django; indeks pgvector dilewati.",
            journal.id,
        )
    return journal


class AdminJournalDetailView(APIView):
    """GET, PUT, DELETE single journal article."""
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    def get(self, request, journal_id):
        """Get single journal detail."""
        try:
            journal = JournalArticle.objects.get(id=journal_id)
            return Response({
                'journal': JournalArticleSerializer(journal).data
            })
        except JournalArticle.DoesNotExist:
            return Response({'error': 'Journal not found'}, status=404)
    
    def put(self, request, journal_id):
        """Update journal article."""
        try:
            journal = JournalArticle.objects.get(id=journal_id)
            
            # Update fields
            for field in ['title', 'abstract', 'authors', 'doi', 'url', 'publisher', 
                         'journal_name', 'published_date', 'source_portal', 'keywords']:
                if field in request.data:
                    setattr(journal, field, request.data[field] or getattr(journal, field))
            
            journal.save()
            
            return Response({
                'message': 'Journal updated successfully',
                'journal': JournalArticleSerializer(journal).data
            })
        except JournalArticle.DoesNotExist:
            return Response({'error': 'Journal not found'}, status=404)
        except Exception as e:
            logger.error(f"Error updating journal: {e}")
            return Response({'error': str(e)}, status=500)
    
    def delete(self, request, journal_id):
        """Delete journal article."""
        try:
            journal = JournalArticle.objects.get(id=journal_id)
            title = journal.title
            journal.delete()
            
            return Response({
                'message': f'Journal "{title[:50]}..." deleted successfully'
            })
        except JournalArticle.DoesNotExist:
            return Response({'error': 'Journal not found'}, status=404)
        except Exception as e:
            logger.error(f"Error deleting journal: {e}")
            return Response({'error': str(e)}, status=500)


class AdminJournalImportView(APIView):
    """Bulk import journals from file or API."""
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    def post(self, request):
        """Bulk import journals."""
        journals_data = request.data.get('journals', [])
        
        if not journals_data:
            return Response({'error': 'No journals provided'}, status=400)
        
        created_count = 0
        errors = []
        
        for idx, data in enumerate(journals_data):
            try:
                # Skip if DOI already exists
                if data.get('doi') and JournalArticle.objects.filter(doi=data['doi']).exists():
                    errors.append(f"Journal {idx+1}: DOI already exists")
                    continue
                
                journal = JournalArticle.objects.create(
                    title=data.get('title', ''),
                    abstract=data.get('abstract', ''),
                    authors=data.get('authors', ''),
                    doi=data.get('doi') or None,
                    url=data.get('url') or None,
                    publisher=data.get('publisher', ''),
                    journal_name=data.get('journal_name', ''),
                    source_portal=data.get('source_portal', 'other'),
                    keywords=data.get('keywords', ''),
                    created_by=request.user
                )
                created_count += 1
                
            except Exception as e:
                errors.append(f"Journal {idx+1}: {str(e)}")
        
        return Response({
            'message': f'Imported {created_count} journals',
            'created_count': created_count,
            'errors': errors
        }, status=201 if created_count > 0 else 400)
