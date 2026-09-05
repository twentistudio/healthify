"""
Health Intelligence Engine — orkestrator (§4, §5, §12).

    Health Input
          v
    Health Understanding      (query_understanding + context)
          v
    Evidence Retrieval        (retrieval + evidence.selector)
          v
    Evidence-Grounded Reasoning (claims / reasoning)
          v
    Safety Validation         (safety)
          v
    Response

Engine ini adalah kapabilitas TAMBAHAN. Produk Healthify yang sudah ada
(`/api/verify/`, dispute, admin, knowledge base) tetap berjalan tanpa
menyentuh modul ini sama sekali.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from .claims.evaluator import evaluate_claim
from .context.evidence_memory import (
    can_answer_from_memory,
    recent_evidence,
    topic_changed,
)
from .context.conversation import (
    build_effective_query,
    load_state,
    persist_turn,
    rebuild_context_from_history,
)
from .context.extractor import context_terms, extract_health_context
from .contracts import (
    EvidenceStatus,
    HealthContext,
    IntelligenceRequest,
    IntelligenceResponse,
    Intent,
    Mode,
    SafetyDecision,
    SupportedClaim,
)
from .evidence.provenance import attribute_claims, unsupported_claims
from .evidence.selector import select_evidence
from .query_understanding.classifier import classify_intent
from .reasoning.generator import (
    _normalize_citation_placement as normalize_citation_placement,
    build_preliminary_assessment,
    generate_response,
)
from .retrieval.acquisition import (
    build_topic_phrase,
    coverage_is_thin,
    ensure_coverage,
)
from .retrieval.concepts import extract_health_concepts
from .retrieval.retriever import rescore_for_query, retrieve_candidates
from .safety.validator import validate_response

logger = logging.getLogger(__name__)

ENGINE_VERSION = "1.0.0"

# Intent yang dijalankan lewat claim engine (§11)
_CLAIM_INTENTS = {Intent.CLAIM_VERIFICATION}


def _uncertainty_message(status: EvidenceStatus, intent: Intent) -> Optional[str]:
    if status == EvidenceStatus.INSUFFICIENT_EVIDENCE:
        return (
            "Bukti yang tersedia di basis pengetahuan belum memadai untuk menjawab "
            "pertanyaan ini secara bertanggung jawab."
        )
    if status == EvidenceStatus.PARTIAL:
        return (
            "Bukti yang ditemukan terbatas jumlah atau relevansinya, sehingga "
            "kesimpulan di atas belum dapat dianggap final."
        )
    return None


# Intent yang tidak membutuhkan pencarian literatur sama sekali.
_NO_RETRIEVAL_INTENTS = (Intent.UNSUPPORTED, Intent.SMALL_TALK)


def process(payload: Dict[str, Any], consumer: str = "healthify") -> IntelligenceResponse:
    """
    Titik masuk tunggal Health Intelligence Engine.

    Args:
        payload: dict sesuai kontrak §6 (query, mode, context, options)
        consumer: identitas pemanggil ("healthify", "healthtalk", ...)
    """
    started = time.time()
    request = IntelligenceRequest.from_payload(payload)

    # ------------------------------------------------------------------
    # 1. Conversation context (§9)
    # ------------------------------------------------------------------
    state = load_state(
        conversation_id=request.conversation_id,
        previous_messages=request.previous_messages,
        health_context_payload=request.health_context,
        consumer=consumer,
    )
    # Dibangun ulang HANYA bila sesi ini memang belum punya snapshot, mis. saat
    # consumer mengirim riwayat sendiri lewat `previous_messages`. Snapshot yang
    # kosong bukan snapshot yang hilang: itu keadaan sesudah pembahasan
    # berpindah penyakit, dan membangunnya ulang dari riwayat justru
    # mengembalikan topik yang baru saja ditinggalkan.
    if not state.has_snapshot and state.health_context.is_empty() and state.messages:
        state.health_context = rebuild_context_from_history(state)

    # ------------------------------------------------------------------
    # 2. Query understanding (§7)
    # ------------------------------------------------------------------
    intent_result = classify_intent(
        request.query, mode=request.mode, previous_messages=state.messages
    )
    intent = intent_result.intent

    # ------------------------------------------------------------------
    # 3. Structured health context (§8) — akumulatif lintas giliran
    # ------------------------------------------------------------------
    context: HealthContext = extract_health_context(
        request.query, previous=state.health_context
    )

    # ------------------------------------------------------------------
    # 4. Evidence retrieval (§10)
    # ------------------------------------------------------------------
    effective_query = build_effective_query(request.query, state, intent)
    terms = context_terms(context)

    # Di dalam ruang obrolan, satu topik dibahas lewat banyak gelembung pesan.
    # Mengulang pencarian dari nol pada setiap pertanyaan membuat jurnal yang
    # terpilih berganti-ganti antar giliran, sehingga jawaban tampak berubah
    # pendirian untuk pembahasan yang sama.
    #
    # Urutannya: pertanyaan pertama selalu mencari. Pertanyaan lanjutan yang
    # masih terjawab oleh jurnal percakapan ini memakai jurnal itu lagi. Yang
    # sudah di luar jangkauannya memicu pencarian baru.
    evidence_source = "retrieval"
    remembered = []
    if intent not in _NO_RETRIEVAL_INTENTS and state.session is not None:
        try:
            remembered = recent_evidence(state.session)
        except Exception as exc:  # pragma: no cover
            logger.warning("[ENGINE] ingatan bukti gagal dibaca: %s", exc)
            remembered = []

    # Pertanyaan ASLI, bukan yang sudah diperkaya konteks: query yang diperkaya
    # membawa topik sebelumnya, sehingga percakapan tidak akan pernah bisa
    # berpindah pembahasan.
    reuse = bool(remembered) and can_answer_from_memory(request.query, remembered)

    # Ketika pengguna menyebut penyakit lain, konteks percakapan sebelumnya
    # justru menyesatkan pencarian: istilah topik lama yang ikut ditempelkan ke
    # query menenggelamkan penyakit yang baru disebut, sehingga pencarian
    # "baru" mengembalikan jurnal yang sama persis. Untuk giliran itu
    # pertanyaannya dipakai apa adanya.
    if remembered and not reuse and topic_changed(request.query, remembered):
        logger.info("[ENGINE] pembahasan berpindah; konteks topik lama dilepas")
        # Konteks kesehatan bersifat akumulatif lintas giliran, dan itu memang
        # yang diinginkan selama satu pembahasan. Begitu pengguna berpindah
        # penyakit, akumulasi itu berubah menjadi beban: istilah topik lama
        # ikut menempel pada setiap giliran berikutnya dan terus menarik jurnal
        # yang sudah tidak dibicarakan. Konteks dimulai ulang dari pertanyaan
        # yang membuka topik baru.
        context = extract_health_context(request.query)
        effective_query = request.query
        terms = context_terms(context)

    candidates = []
    if reuse:
        evidence_source = "conversation"
        # Dinilai ulang terhadap pertanyaan yang baru: baris dari basis data
        # tidak membawa skor, dan jurnal yang ternyata sudah tidak nyambung
        # harus tetap tersaring seperti pada pencarian biasa.
        candidates = rescore_for_query(remembered, effective_query, extra_terms=terms)
        logger.info("[ENGINE] %d bukti percakapan dipakai ulang (tanpa pencarian baru)",
                    len(remembered))
    elif intent not in _NO_RETRIEVAL_INTENTS:
        try:
            candidates = retrieve_candidates(effective_query, extra_terms=terms)
        except Exception as exc:  # pragma: no cover
            logger.error("[ENGINE] retrieval gagal: %s", exc, exc_info=True)
            candidates = []

    # ------------------------------------------------------------------
    # 5. Evidence validation & selection (§14, §15, §16)
    # ------------------------------------------------------------------
    evidence, evidence_status = select_evidence(
        candidates, context_terms=terms, limit=request.max_evidence,
        # Tautan bukti yang dipakai ulang sudah diperiksa pada giliran
        # sebelumnya dalam percakapan yang sama; memeriksanya lagi hanya
        # menambah waktu tunggu tanpa mengubah hasil.
        validate=not reuse,
    )

    # Pertanyaan kesehatan yang sah tetapi tidak menemukan bukti berarti ada
    # lubang di basis pengetahuan, bukan pertanyaan yang buruk. Sebelumnya
    # lubang itu hanya bisa ditutup oleh manusia yang kebetulan menyadarinya
    # lalu menjalankan `import_journals`; konsumen eksternal tidak punya siapa
    # pun yang mengawasi. Mesin menutupnya sendiri, lalu mencoba sekali lagi.
    #
    # Jurnal yang baru masuk melewati gerbang relevansi yang sama persis, jadi
    # menambah bahan bacaan tidak pernah melonggarkan syarat apa pun.
    # Penilaian ulang bisa menyisakan nol bila jurnal lama ternyata tidak lagi
    # menjawab. Percakapan tidak boleh berakhir tanpa rujukan hanya karena
    # sempat memilih jalur ingatan; pencarian biasa dijalankan sebagai gantinya.
    if reuse and not evidence and intent not in _NO_RETRIEVAL_INTENTS:
        logger.info("[ENGINE] ingatan tidak lagi menjawab; kembali mencari")
        reuse = False
        evidence_source = "retrieval"
        try:
            candidates = retrieve_candidates(effective_query, extra_terms=terms)
            evidence, evidence_status = select_evidence(
                candidates, context_terms=terms, limit=request.max_evidence)
        except Exception as exc:  # pragma: no cover
            logger.error("[ENGINE] retrieval gagal: %s", exc, exc_info=True)

    #
    # Pemicunya bukan hanya bukti kosong. Pertanyaan tentang skabies bisa saja
    # menarik lima paper penyakit kulit lain dan lolos sebagai "cukup", padahal
    # tak satu pun membahas skabies. Cakupan yang tipis diperlakukan sama
    # dengan tidak ada cakupan.
    if not reuse and intent not in _NO_RETRIEVAL_INTENTS and (
            evidence_status == EvidenceStatus.INSUFFICIENT_EVIDENCE
            or coverage_is_thin(effective_query, evidence)):
        try:
            added = ensure_coverage(effective_query, health_checked=True)
        except Exception as exc:  # pragma: no cover
            logger.warning("[ENGINE] pelengkapan basis pengetahuan gagal: %s", exc)
            added = 0

        # Percobaan ulang memakai istilah Inggris hasil terjemahan, dan berjalan
        # baik ketika ada jurnal baru maupun tidak. Jurnal yang diambil pada
        # pertanyaan sebelumnya tetap tak terjangkau bila percobaan berikutnya
        # mengulang pertanyaan Indonesia yang sejak awal gagal menemukannya.
        # Frasa ini sudah ada di cache dari proses pengambilan, jadi tidak
        # menambah biaya.
        topic = build_topic_phrase(effective_query)
        if added or topic:
            logger.info("[ENGINE] %d jurnal baru; pengambilan diulang dengan istilah Inggris",
                        added)
            try:
                retry_terms = list(terms) + topic.split()
                candidates = retrieve_candidates(effective_query, extra_terms=retry_terms)
                evidence, evidence_status = select_evidence(
                    candidates, context_terms=terms, limit=request.max_evidence
                )
            except Exception as exc:  # pragma: no cover
                logger.error("[ENGINE] pengambilan ulang gagal: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # 6. Reasoning (§11 claim / §12 consultation)
    # ------------------------------------------------------------------
    claim_evaluation = None
    if intent in _CLAIM_INTENTS:
        claim_evaluation = evaluate_claim(request.query, evidence, evidence_status)
        answer = claim_evaluation.explanation
        gen_meta = {"generator": f"claim_engine:{claim_evaluation.method}"}
        # Jalur claim engine juga menghasilkan teks yang dibaca manusia, jadi
        # rujukan sumber di dalamnya harus dinormalkan sama seperti jalur lain.
        answer = normalize_citation_placement(answer)
    else:
        answer, gen_meta = generate_response(
            query=request.query,
            intent=intent,
            context=context,
            evidence=evidence,
            evidence_status=evidence_status,
            previous_messages=state.messages,
            language=request.language,
        )

    # ------------------------------------------------------------------
    # 7. Claim provenance (§14)
    # ------------------------------------------------------------------
    # Jawaban template sistem (mis. "bukti tidak memadai", "di luar cakupan")
    # bukan pernyataan medis, jadi tidak perlu ditelusuri ke evidence.
    is_template_answer = str(gen_meta.get("generator", "")).startswith("template_")
    attributed: List[SupportedClaim] = (
        [] if is_template_answer else attribute_claims(answer, evidence)
    )

    if claim_evaluation is not None:
        attributed.insert(0, SupportedClaim(
            claim=request.query,
            supporting_evidence=[
                {
                    "chunk_id": e.chunk_id,
                    "source_id": e.source_id or e.chunk_id,
                    "title": e.title,
                    "doi": e.doi or None,
                    "url": e.url or None,
                }
                for e in evidence
                if (e.source_id or e.chunk_id) in claim_evaluation.supporting_evidence_ids
            ] or [
                {
                    "chunk_id": e.chunk_id,
                    "source_id": e.source_id or e.chunk_id,
                    "title": e.title,
                    "doi": e.doi or None,
                    "url": e.url or None,
                }
                for e in evidence[:3]
            ],
            verdict=claim_evaluation.verdict,
            confidence=claim_evaluation.confidence,
        ))

    # ------------------------------------------------------------------
    # 8. Safety layer (§17)
    # ------------------------------------------------------------------
    generated_claims = [c for c in attributed if c.claim != request.query]
    safety = validate_response(
        answer=answer,
        user_input=request.query,
        evidence_status=evidence_status,
        unsupported_claims=unsupported_claims(generated_claims),
        intent=intent,
        total_claims=len(generated_claims),
    )
    answer = safety.answer or answer

    # ------------------------------------------------------------------
    # 9. Preliminary assessment (§18)
    # ------------------------------------------------------------------
    preliminary = None
    if request.mode in (Mode.CONSULTATION,) or intent in (
        Intent.SYMPTOM_CONTEXT, Intent.FOLLOW_UP
    ):
        preliminary = build_preliminary_assessment(
            context, evidence, evidence_status, safety.flags
        )

    # ------------------------------------------------------------------
    # 10. Persist & respond
    # ------------------------------------------------------------------
    conversation_id = request.conversation_id
    if conversation_id:
        persist_turn(
            conversation_id=conversation_id,
            consumer=consumer,
            user_message=request.query,
            assistant_message=answer,
            health_context=context,
            intent=intent,
            evidence_status=evidence_status.value,
            safety_decision=safety.decision.value,
            evidence_refs=[
                {"source_id": e.source_id, "doi": e.doi or None, "title": e.title}
                for e in evidence
            ],
        )

    elapsed_ms = int((time.time() - started) * 1000)
    metadata = {
        "engine_version": ENGINE_VERSION,
        "consumer": consumer,
        "request_id": uuid.uuid4().hex,
        "processing_time_ms": elapsed_ms,
        "intent_confidence": round(intent_result.confidence, 3),
        "intent_signals": intent_result.signals[:6],
        "is_health_related": intent_result.is_health_related,
        "effective_query": effective_query[:300],
        "candidates_retrieved": len(candidates),
        "evidence_selected": len(evidence),
        # Memberi tahu consumer apakah giliran ini bersandar pada jurnal yang
        # sama dengan giliran sebelumnya, sehingga rujukan dapat ditampilkan
        # konsisten di dalam satu ruang obrolan.
        "evidence_source": evidence_source,
        "safety": safety.to_dict(),
        **gen_meta,
    }
    if claim_evaluation is not None:
        metadata["claim_evaluation"] = claim_evaluation.to_dict()

    response = IntelligenceResponse(
        answer=answer,
        intent=intent,
        mode=request.mode,
        health_context=context,
        evidence=evidence if request.include_evidence else [],
        claims=attributed,
        uncertainty=_uncertainty_message(evidence_status, intent),
        evidence_status=evidence_status,
        safety_decision=safety.decision,
        safety_flags=safety.flags,
        preliminary_assessment=preliminary,
        conversation_id=conversation_id,
        metadata=metadata,
    )

    logger.info(
        "[ENGINE] intent=%s evidence=%d status=%s safety=%s %dms",
        intent.value, len(evidence), evidence_status.value,
        safety.decision.value, elapsed_ms,
    )
    return response
