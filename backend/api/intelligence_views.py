"""
Integration/API layer untuk Health Intelligence Engine (§4, §21).

    HealthTalk -> Healthify API -> Health Intelligence Engine -> Response

Endpoint di file ini adalah KAPABILITAS TAMBAHAN. Tidak satu pun endpoint
Healthify yang sudah ada (`/api/verify/`, `/api/claims/`, dispute, admin)
diubah atau dipindahkan. Healthify tetap berjalan penuh tanpa endpoint ini.

Consumer eksternal TIDAK PERNAH menyentuh database Healthify secara langsung —
semua akses melalui kontrak di bawah ini.
"""

import json
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .intelligence import engine
from .intelligence.adapters.healthtalk import to_consumer_response, to_simple_response
from .intelligence.contracts import Mode
from .intelligence.summarization.summarizer import build_summary, persist_summary

logger = logging.getLogger(__name__)

API_VERSION = "v1"
MAX_QUERY_LENGTH = 5000


def request_correlation_id(request) -> str:
    """
    ID korelasi untuk satu permintaan.

    Consumer backend biasanya sudah punya trace id sendiri; bila dikirim lewat
    `X-Request-Id`, nilai itu dipakai apa adanya sehingga log kedua sistem bisa
    disandingkan. Bila tidak, Healthify membuatkan.
    """
    import re
    import uuid

    supplied = (request.META.get("HTTP_X_REQUEST_ID") or "").strip()[:64]
    if supplied and re.fullmatch(r"[A-Za-z0-9._:-]{8,64}", supplied):
        return supplied
    return uuid.uuid4().hex


def _idempotency_cache_key(request) -> str:
    """Kunci idempotensi, dipisah per API key agar tidak bertabrakan antar consumer."""
    import hashlib

    key = (request.META.get("HTTP_X_IDEMPOTENCY_KEY") or "").strip()[:128]
    if not key:
        return ""
    api_key = request.META.get("HTTP_X_API_KEY", "")
    scope = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"idempotency:v1:{scope}:{digest}"


class ConsumerRateThrottle(SimpleRateThrottle):
    """
    Batasi laju per consumer (per API key), bukan per alamat IP.

    Tanpa ini satu consumer dapat menghabiskan kuota LLM/embedding milik
    Healthify. Kunci throttle memakai API key bila ada, sehingga beberapa
    consumer tidak saling menghabiskan jatah.
    """

    scope = "intelligence"

    def allow_request(self, request, view):
        # Batas khusus per API key bila dikonfigurasi; selain itu batas global.
        per_key = getattr(settings, "INTELLIGENCE_KEY_RATES", {}) or {}
        api_key = request.META.get("HTTP_X_API_KEY", "").strip()
        override = per_key.get(api_key)
        if override:
            self.rate = override
            self.num_requests, self.duration = self.parse_rate(override)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        api_key = request.META.get("HTTP_X_API_KEY", "").strip()
        if api_key:
            import hashlib
            ident = hashlib.sha256(api_key.encode()).hexdigest()[:32]
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


# ---------------------------------------------------------------------------
# Autentikasi consumer
# ---------------------------------------------------------------------------

def _database_keys_exist() -> bool:
    """
    Apakah ada kunci aktif di database.

    Dipisah agar endpoint tidak berubah menjadi terbuka ketika variabel
    lingkungan kosong padahal kunci sudah diterbitkan lewat `issue_api_key`.
    Kegagalan database dibaca sebagai "tidak ada", supaya masalah penyimpanan
    tidak berubah menjadi celah autentikasi terbuka; permintaan tanpa kunci
    tetap ditolak selama variabel lingkungan terisi.
    """
    from .models import IntelligenceApiKey

    try:
        return IntelligenceApiKey.objects.filter(is_active=True).exists()
    except Exception:
        return False


def _consumer_for_stored_key(api_key: str):
    """
    Cocokkan kunci dengan tabel. Yang dibandingkan hash-nya, karena nilai asli
    kunci memang tidak pernah disimpan.
    """
    from django.utils import timezone

    from .models import IntelligenceApiKey

    try:
        record = IntelligenceApiKey.objects.filter(
            key_hash=IntelligenceApiKey.hash_key(api_key), is_active=True
        ).first()
    except Exception:
        return None

    if not record:
        return None

    try:
        IntelligenceApiKey.objects.filter(pk=record.pk).update(last_used_at=timezone.now())
    except Exception:
        pass

    # `ConsumerRateThrottle` mencari batas khusus berdasarkan API key, bukan
    # nama konsumen, jadi kuncinya harus sama persis dengan yang dibaca di sana.
    if record.rate:
        rates = getattr(settings, "INTELLIGENCE_KEY_RATES", None)
        if isinstance(rates, dict):
            rates[api_key] = record.rate

    return record.consumer


def resolve_consumer(request):
    """
    Tentukan identitas consumer dari header.

    - Bila `settings.INTELLIGENCE_API_KEYS` diisi ({api_key: consumer_name}),
      header `X-API-Key` WAJIB dan harus cocok.
    - Bila tidak dikonfigurasi, endpoint terbuka (mode pengembangan) dan
      identitas diambil dari `X-Consumer` (default: "healthtalk").

    Returns: (consumer_name, error_response|None)
    """
    configured = getattr(settings, "INTELLIGENCE_API_KEYS", None) or {}
    api_key = request.META.get("HTTP_X_API_KEY", "").strip()

    # Kunci boleh berasal dari dua tempat: variabel lingkungan (cara lama, tetap
    # berlaku) dan tabel `IntelligenceApiKey` yang diisi `issue_api_key`. Tabel
    # memungkinkan satu konsumen memegang banyak kunci sekaligus dan mencabut
    # satu tanpa mematikan yang lain.
    if configured or _database_keys_exist():
        if not api_key:
            return None, Response(
                {"error": "unauthorized", "detail": "Header X-API-Key wajib diisi."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        consumer = configured.get(api_key) or _consumer_for_stored_key(api_key)
        if not consumer:
            return None, Response(
                {"error": "unauthorized", "detail": "API key tidak dikenal."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return consumer, None

    consumer = (request.META.get("HTTP_X_CONSUMER") or "healthtalk").strip()[:64]
    return consumer or "healthtalk", None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class IntelligenceQueryView(APIView):
    """
    POST /api/v1/intelligence/query

    Kirim satu pertanyaan/klaim/keluhan kesehatan dan terima jawaban
    terstruktur yang sudah tervalidasi bukti dan lolos safety layer.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ConsumerRateThrottle]

    def post(self, request):
        consumer, error = resolve_consumer(request)
        if error:
            return error

        payload = request.data if isinstance(request.data, dict) else {}
        query = (payload.get("query") or payload.get("text") or "").strip()

        if not query:
            return Response(
                {"error": "invalid_request", "detail": "Field 'query' wajib diisi."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(query) > MAX_QUERY_LENGTH:
            return Response(
                {
                    "error": "invalid_request",
                    "detail": f"Field 'query' maksimal {MAX_QUERY_LENGTH} karakter.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_mode = payload.get("mode")
        if raw_mode and str(raw_mode).strip().lower() not in {m.value for m in Mode}:
            return Response(
                {
                    "error": "invalid_request",
                    "detail": "Field 'mode' harus salah satu dari: "
                              + ", ".join(m.value for m in Mode),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        correlation_id = request_correlation_id(request)

        # Idempotensi: permintaan yang diulang dengan kunci sama TIDAK diproses
        # ulang. Ini penting bagi consumer backend — satu permintaan bisa makan
        # 2-10 detik, jadi retry setelah timeout wajar terjadi, dan tanpa
        # penjagaan ini retry akan menggandakan giliran percakapan sekaligus
        # membayar dua kali ke penyedia LLM.
        idempotency_key = _idempotency_cache_key(request)
        if idempotency_key:
            cached = cache.get(idempotency_key)
            if cached is not None:
                response = Response(cached, status=status.HTTP_200_OK)
                response["X-Request-Id"] = correlation_id
                response["X-Idempotent-Replay"] = "true"
                return response

        try:
            result = engine.process(payload, consumer=consumer)
        except Exception as exc:
            logger.error("[INTELLIGENCE] pemrosesan gagal (request_id=%s): %s",
                         correlation_id, exc, exc_info=True)
            response = Response(
                {
                    "error": "engine_error",
                    "detail": "Terjadi kesalahan saat memproses permintaan.",
                    "request_id": correlation_id,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
            response["X-Request-Id"] = correlation_id
            return response

        result.metadata["request_id"] = correlation_id

        options = payload.get("options") or {}

        # `format: "simple"` -> hanya jawaban + sumber jurnal, tanpa label
        # maupun metadata internal. Default tetap "full" agar tidak breaking.
        if str(options.get("format", "full")).strip().lower() == "simple":
            body = to_simple_response(result)
        else:
            body = to_consumer_response(
                result,
                include_evidence=bool(options.get("include_evidence", True)),
                include_sources=bool(options.get("include_sources", True)),
            )

        if idempotency_key:
            cache.set(idempotency_key, body,
                      timeout=getattr(settings, "IDEMPOTENCY_TTL", 86400))

        response = Response(body, status=status.HTTP_200_OK)
        response["X-Request-Id"] = correlation_id
        return response


class ConsultationSummaryView(APIView):
    """
    POST /api/v1/intelligence/summary

    Hasilkan ringkasan konsultasi terstruktur dari sebuah sesi percakapan.
    Setiap bagian ringkasan membawa provenance-nya (§20).
    """

    permission_classes = [AllowAny]
    throttle_classes = [ConsumerRateThrottle]

    def post(self, request):
        consumer, error = resolve_consumer(request)
        if error:
            return error

        payload = request.data if isinstance(request.data, dict) else {}
        session_id = (
            payload.get("session_id") or payload.get("conversation_id") or ""
        ).strip()

        if not session_id:
            return Response(
                {"error": "invalid_request", "detail": "Field 'session_id' wajib diisi."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .intelligence.context.conversation import find_session

        # Sesi dicari lewat pembantu bersama, sehingga endpoint ini tidak perlu
        # tahu bagaimana pengenal ruang obrolan dipetakan ke baris, dan satu
        # consumer tidak bisa membaca ruang obrolan milik consumer lain.
        session = find_session(session_id, consumer)
        if not session:
            return Response(
                {"error": "not_found", "detail": f"Sesi '{session_id}' tidak ditemukan."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            summary = build_summary(session)
        except Exception as exc:
            logger.error("[INTELLIGENCE] pembuatan summary gagal: %s", exc, exc_info=True)
            return Response(
                {"error": "engine_error", "detail": "Gagal menyusun ringkasan konsultasi."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if payload.get("persist", True):
            persist_summary(session, summary)

        if payload.get("close_session"):
            from .models import ConversationSession

            session.status = ConversationSession.STATUS_CLOSED
            session.save(update_fields=["status", "updated_at"])
            summary["session_status"] = session.status

        return Response({"summary": summary}, status=status.HTTP_200_OK)


class ConversationSessionView(APIView):
    """
    GET /api/v1/intelligence/sessions/<session_id>

    Ambil riwayat percakapan + health context kumulatif sebuah sesi.
    """

    permission_classes = [AllowAny]

    def get(self, request, session_id):
        consumer, error = resolve_consumer(request)
        if error:
            return error

        from .intelligence.context.conversation import find_session

        # Sesi dicari lewat pembantu bersama, sehingga endpoint ini tidak perlu
        # tahu bagaimana pengenal ruang obrolan dipetakan ke baris, dan satu
        # consumer tidak bisa membaca ruang obrolan milik consumer lain.
        session = find_session(session_id, consumer)
        if not session:
            return Response(
                {"error": "not_found", "detail": f"Sesi '{session_id}' tidak ditemukan."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            health_context = json.loads(session.health_context) if session.health_context else {}
        except (ValueError, TypeError):
            health_context = {}

        return Response(
            {
                "session_id": session.session_id,
                "consumer": session.consumer,
                "status": session.status,
                "health_context": health_context,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "messages": [
                    {
                        "role": message.role,
                        "content": message.content,
                        "intent": message.intent or None,
                        "evidence_status": message.evidence_status or None,
                        "safety_decision": message.safety_decision or None,
                        "created_at": message.created_at.isoformat(),
                    }
                    for message in session.messages.all()
                ],
            },
            status=status.HTTP_200_OK,
        )

    def delete(self, request, session_id):
        """Tutup sesi (tidak menghapus data, hanya menandai selesai)."""
        consumer, error = resolve_consumer(request)
        if error:
            return error

        from .intelligence.context.conversation import find_session

        # Sesi dicari lewat pembantu bersama, sehingga endpoint ini tidak perlu
        # tahu bagaimana pengenal ruang obrolan dipetakan ke baris, dan satu
        # consumer tidak bisa membaca ruang obrolan milik consumer lain.
        session = find_session(session_id, consumer)
        if not session:
            return Response(
                {"error": "not_found", "detail": f"Sesi '{session_id}' tidak ditemukan."},
                status=status.HTTP_404_NOT_FOUND,
            )

        session.status = ConversationSession.STATUS_CLOSED
        session.save(update_fields=["status", "updated_at"])
        return Response({"session_id": session.session_id, "status": session.status})


class IntelligenceCapabilitiesView(APIView):
    """
    GET /api/v1/intelligence/capabilities

    Deskripsi kapabilitas engine — supaya consumer tidak perlu menebak
    nilai enum yang didukung.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        from .intelligence.contracts import EvidenceStatus, Intent, Provenance, SafetyDecision
        from .intelligence.engine import ENGINE_VERSION
        from .intelligence.reasoning import llm

        return Response(
            {
                "engine": "healthify-health-intelligence-engine",
                "engine_version": ENGINE_VERSION,
                "api_version": API_VERSION,
                "modes": [m.value for m in Mode],
                "intents": [i.value for i in Intent],
                "evidence_status": [s.value for s in EvidenceStatus],
                "safety_decisions": [d.value for d in SafetyDecision],
                "provenance_types": [p.value for p in Provenance],
                "features": {
                    "conversation_context": True,
                    "structured_health_context": True,
                    "evidence_grounded_response": True,
                    "claim_verification": True,
                    "claim_provenance": True,
                    "link_verification": getattr(settings, "EVIDENCE_LINK_CHECK_ENABLED", True),
                    "preliminary_assessment": True,
                    "consultation_summary": True,
                    "safety_layer": True,
                },
                "llm_provider": llm.available_provider(),
                "auth": "x-api-key" if getattr(settings, "INTELLIGENCE_API_KEYS", None) else "open",
            },
            status=status.HTTP_200_OK,
        )


class AccessRequestThrottle(SimpleRateThrottle):
    """
    Batasi permintaan akses per alamat IP.

    Endpoint ini terbuka tanpa kunci (memang untuk orang yang belum punya
    kunci), jadi tanpa pembatasan ia menjadi sasaran empuk pengiriman massal.
    """

    scope = "access_request"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AccessRequestView(APIView):
    """
    POST /api/v1/intelligence/access-request

    Formulir permintaan akses dari halaman dokumentasi. Menyimpan permintaan
    untuk ditinjau manusia; TIDAK menerbitkan kunci apa pun. Kunci diterbitkan
    operator dengan `python manage.py issue_api_key`, sehingga pengisi formulir
    tidak pernah bisa memberi akses kepada dirinya sendiri.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AccessRequestThrottle]

    MAX_LENGTHS = {
        "name": 200,
        "email": 254,
        "organization": 200,
        "use_case": 2000,
        "expected_volume": 120,
    }

    def post(self, request):
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email

        from .models import ApiAccessRequest

        payload = request.data if isinstance(request.data, dict) else {}

        def field(name):
            return str(payload.get(name) or "").strip()[:self.MAX_LENGTHS[name]]

        name = field("name")
        email = field("email")
        use_case = field("use_case")

        missing = [k for k, v in (("name", name), ("email", email), ("use_case", use_case)) if not v]
        if missing:
            return Response(
                {"error": "invalid_request",
                 "detail": f"Field wajib belum diisi: {', '.join(missing)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_email(email)
        except ValidationError:
            return Response(
                {"error": "invalid_request", "detail": "Alamat email tidak valid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        access_request = ApiAccessRequest.objects.create(
            name=name,
            email=email,
            organization=field("organization"),
            use_case=use_case,
            expected_volume=field("expected_volume"),
        )
        logger.info("[ACCESS] permintaan akses baru #%s dari %s", access_request.id, email)

        # Pemberitahuan ke operator. Kegagalan di sini tidak boleh menular ke
        # respons: permintaannya sudah tersimpan, dan pemohon berhak menerima
        # konfirmasi itu apa pun yang terjadi dengan surelnya.
        try:
            from .email_service import email_service

            email_service.notify_admin_access_request(access_request)
        except Exception as exc:  # pragma: no cover
            logger.warning("[ACCESS] pemberitahuan operator gagal: %s", exc)

        return Response(
            {
                "status": "received",
                "request_id": access_request.id,
                "detail": "Your request has been recorded. We will contact you by email.",
            },
            status=status.HTTP_201_CREATED,
        )
