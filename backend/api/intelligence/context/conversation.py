"""
Conversation context (§9).

Engine harus memahami pesan sebagai bagian dari sebuah percakapan, bukan query
independen. Modul ini:
  1. memuat/menyimpan riwayat percakapan + HealthContext kumulatif,
  2. menyusun "effective query" untuk retrieval, yaitu query user yang sudah
     diperkaya konteks sebelumnya (mis. "Apakah itu normal?" ->
     "demam 3 hari batuk apakah normal").

Penyimpanan bersifat opsional: bila tidak ada `conversation_id`, engine tetap
jalan secara stateless memakai `previous_messages` yang dikirim caller.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from ..contracts import HealthContext, Intent
from .extractor import context_terms, extract_health_context, health_context_from_dict

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 20
MAX_CONTEXT_MESSAGES = 8


def storage_key(consumer: str, conversation_id: str) -> str:
    """
    Kunci penyimpanan sesi, dipisah per consumer.

    Pengenal ruang obrolan dibuat oleh produk lain, dan dua produk bisa
    sama-sama memakai "room-1" tanpa saling tahu. Tanpa pemisahan ini percakapan
    milik satu produk akan terbaca oleh produk lain — kebocoran yang tidak
    kentara justru karena tampak "berfungsi".

    Yang dikembalikan ke consumer tetap pengenal aslinya; awalan ini hanya
    hidup di dalam basis data.
    """
    consumer = (consumer or "healthify").strip() or "healthify"
    return f"{consumer}:{conversation_id}"


class ConversationState:
    """Kumpulan riwayat + health context untuk satu percakapan."""

    def __init__(self, conversation_id: Optional[str] = None,
                 messages: Optional[List[Dict[str, str]]] = None,
                 health_context: Optional[HealthContext] = None,
                 session=None, has_snapshot: bool = False):
        self.conversation_id = conversation_id
        self.messages: List[Dict[str, str]] = list(messages or [])
        self.health_context: HealthContext = health_context or HealthContext()
        self.session = session  # ConversationSession | None
        # Apakah sesi ini punya snapshot konteks tersimpan. Dibedakan dari
        # "konteks kosong": snapshot yang memang kosong terjadi setelah
        # pembahasan berpindah penyakit, dan membangunnya ulang dari riwayat
        # akan mengembalikan topik yang baru saja ditinggalkan.
        self.has_snapshot = has_snapshot

    @property
    def is_new(self) -> bool:
        return not self.messages

    def user_messages(self) -> List[str]:
        return [m["content"] for m in self.messages if m.get("role") == "user"]

    def last_assistant_message(self) -> str:
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                return msg.get("content", "")
        return ""

    def to_dict(self) -> Dict:
        return {
            "conversation_id": self.conversation_id,
            "turns": len(self.messages),
            "health_context": self.health_context.to_dict(),
        }


def public_conversation_id(session) -> str:
    """
    Pengenal ruang obrolan sebagaimana consumer mengenalnya.

    Awalan consumer adalah urusan internal penyimpanan. Membocorkannya ke
    payload membuat consumer menerima pengenal yang berbeda dari yang mereka
    kirim, dan kalau dipakai kembali akan menunjuk sesi yang salah.
    """
    raw = getattr(session, "session_id", "") or ""
    prefix = f"{getattr(session, 'consumer', '') or ''}:"
    return raw[len(prefix):] if prefix != ":" and raw.startswith(prefix) else raw


def find_session(conversation_id: str, consumer: str = "healthify"):
    """
    Temukan sesi milik satu consumer.

    Satu-satunya tempat yang tahu bagaimana pengenal ruang obrolan dipetakan
    ke baris di basis data. Semua pemanggil memakai ini, sehingga penamaan
    kunci tidak pernah tersebar dan tidak bisa berbeda-beda antar endpoint.
    """
    if not conversation_id:
        return None
    try:
        from ...models import ConversationSession

        return (
            ConversationSession.objects.filter(
                session_id=storage_key(consumer, conversation_id)).first()
            # Sesi lama tersimpan tanpa awalan consumer.
            or ConversationSession.objects.filter(session_id=conversation_id).first()
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("[CONVERSATION] gagal mencari sesi %s: %s", conversation_id, exc)
        return None


def load_state(conversation_id: Optional[str],
               previous_messages: Optional[List[Dict[str, str]]] = None,
               health_context_payload: Optional[Dict] = None,
               consumer: str = "healthify") -> ConversationState:
    """
    Muat state percakapan.

    Prioritas sumber riwayat:
      1. Riwayat tersimpan di DB (bila `conversation_id` dikenal)
      2. `previous_messages` yang dikirim caller (stateless mode)

    Keduanya digabung: pesan dari caller yang belum tersimpan tetap dipakai.
    """
    stored_messages: List[Dict[str, str]] = []
    stored_context = HealthContext()
    has_snapshot = False
    session = None

    if conversation_id:
        try:
            session = find_session(conversation_id, consumer)
            if session:
                stored_messages = [
                    {"role": m.role, "content": m.content}
                    for m in session.messages.all()[:MAX_HISTORY_TURNS * 2]
                ]
                if session.health_context:
                    try:
                        stored_context = health_context_from_dict(json.loads(session.health_context))
                        has_snapshot = True
                    except (ValueError, TypeError) as exc:
                        logger.warning("[CONVERSATION] health_context rusak untuk %s: %s",
                                       conversation_id, exc)
        except Exception as exc:  # pragma: no cover - DB belum termigrasi dsb
            logger.warning("[CONVERSATION] gagal memuat sesi %s: %s", conversation_id, exc)

    caller_messages = list(previous_messages or [])
    if stored_messages:
        known = {(m["role"], m["content"]) for m in stored_messages}
        extra = [m for m in caller_messages if (m.get("role"), m.get("content")) not in known]
        messages = stored_messages + extra
    else:
        messages = caller_messages

    # Health context eksplisit dari caller menimpa/menambah yang tersimpan.
    if health_context_payload:
        stored_context = stored_context.merge(health_context_from_dict(health_context_payload))

    return ConversationState(
        conversation_id=conversation_id,
        messages=messages[-MAX_HISTORY_TURNS * 2:],
        health_context=stored_context,
        session=session,
        has_snapshot=has_snapshot,
    )


def rebuild_context_from_history(state: ConversationState) -> HealthContext:
    """
    Bangun ulang health context dari seluruh pesan user pada percakapan.
    Dipakai saat sesi dimuat dari `previous_messages` (belum ada snapshot).
    """
    context = state.health_context
    for message in state.user_messages():
        context = extract_health_context(message, previous=context)
    return context


def build_effective_query(query: str, state: ConversationState,
                          intent: Optional[Intent] = None) -> str:
    """
    Susun query untuk retrieval dengan menyertakan konteks percakapan.

    Contoh:
        history: "Saya demam", "Sudah tiga hari"
        query  : "Apakah itu normal?"
        hasil  : "Apakah itu normal? demam 3 hari"
    """
    query = (query or "").strip()
    parts: List[str] = [query] if query else []

    terms = context_terms(state.health_context)
    low_query = query.lower()
    for term in terms:
        if term and term not in low_query:
            parts.append(term)

    duration = state.health_context.duration
    if duration and duration.lower() not in low_query:
        parts.append(duration)

    # Untuk follow-up, ikutkan pesan user terakhir yang substantif.
    if intent == Intent.FOLLOW_UP:
        for message in reversed(state.user_messages()):
            if message.strip() and message.strip().lower() != low_query and len(message.split()) > 2:
                snippet = message.strip()[:160]
                if snippet.lower() not in " ".join(parts).lower():
                    parts.append(snippet)
                break

    effective = " ".join(p for p in parts if p).strip()
    return effective or query


def persist_turn(conversation_id: Optional[str],
                 consumer: str,
                 user_message: str,
                 assistant_message: str,
                 health_context: HealthContext,
                 intent: Optional[Intent] = None,
                 evidence_status: str = "",
                 safety_decision: str = "",
                 evidence_refs: Optional[List[Dict]] = None) -> Optional[str]:
    """
    Simpan satu giliran percakapan. Aman dipanggil tanpa conversation_id
    (langsung return None) dan tidak pernah melempar exception ke pipeline.
    """
    if not conversation_id:
        return None
    try:
        from ...models import ConversationMessage, ConversationSession

        # Sesi dibuat sendiri saat pertama kali dipakai. Konsumen tidak perlu
        # mendaftarkan ruang obrolan lebih dulu: cukup kirim pengenal miliknya.
        session = find_session(conversation_id, consumer)
        if session is None:
            session = ConversationSession.objects.create(
                session_id=storage_key(consumer, conversation_id),
                consumer=consumer or "healthify")
        session.health_context = json.dumps(health_context.to_dict(), ensure_ascii=False)
        session.save(update_fields=["health_context", "updated_at"])

        if user_message:
            ConversationMessage.objects.create(
                session=session,
                role=ConversationMessage.ROLE_USER,
                content=user_message,
                intent=(intent.value if isinstance(intent, Intent) else (intent or "")),
            )
        if assistant_message:
            ConversationMessage.objects.create(
                session=session,
                role=ConversationMessage.ROLE_ASSISTANT,
                content=assistant_message,
                intent=(intent.value if isinstance(intent, Intent) else (intent or "")),
                evidence_status=evidence_status or "",
                safety_decision=safety_decision or "",
                evidence_refs=json.dumps(evidence_refs or [], ensure_ascii=False),
            )
        return conversation_id
    except Exception as exc:  # pragma: no cover
        logger.warning("[CONVERSATION] gagal menyimpan giliran %s: %s", conversation_id, exc)
        return None
