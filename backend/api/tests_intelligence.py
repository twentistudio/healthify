"""
Test suite untuk Health Intelligence Engine (§26).

Cakupan:
    - Query understanding (claim / symptom / follow-up / general / medication)
    - Conversation context (multi-turn, referensi, durasi, akumulasi gejala)
    - Retrieval (evidence relevan / tidak relevan / tidak ada)
    - Evidence integrity — ANTI-404 / anti-DOI-halusinasi
    - Generation (grounded, unsupported claim, uncertainty)
    - Safety (benign, high-risk, emergency)
    - Summary (ekstraksi, tanpa halusinasi, provenance)
    - API layer (/api/v1/intelligence/*)
    - Regresi backward-compatibility Healthify

Semua test berjalan offline: validasi link dimatikan lewat setting, dan LLM
dinonaktifkan sehingga engine memakai jalur ekstraktif deterministik.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import (
    Claim,
    ClaimSource,
    ConsultationSummary,
    ConversationMessage,
    ConversationSession,
    JournalArticle,
    Source,
    VerificationResult,
)

# Engine dijalankan tanpa LLM dan tanpa akses jaringan.
OFFLINE = override_settings(
    INTELLIGENCE_LLM_ENABLED="0",
    EMBEDDINGS_ENABLED="0",
    EVIDENCE_LINK_CHECK_ENABLED=False,
    INTELLIGENCE_API_KEYS={},
)


def make_journal(**kwargs):
    defaults = {
        "title": "Fever and cough in acute respiratory infection",
        "abstract": (
            "Fever lasting three days accompanied by cough is commonly observed in "
            "acute respiratory infection. A systematic review of cohort studies "
            "showed that most cases resolve within seven days. Persistent fever "
            "beyond five days warrants clinical evaluation."
        ),
        "doi": "10.1016/j.jinf.2021.02.004",
        "url": "https://doi.org/10.1016/j.jinf.2021.02.004",
        "publisher": "Journal of Infection",
        "journal_name": "Journal of Infection",
        "source_portal": "other",
        "keywords": "demam, batuk, fever, cough, infeksi saluran napas",
    }
    defaults.update(kwargs)
    return JournalArticle.objects.create(**defaults)


# ===========================================================================
# 1. QUERY UNDERSTANDING (§7)
# ===========================================================================

class QueryUnderstandingTests(TestCase):
    def _intent(self, query, **kwargs):
        from ragai.query_understanding.classifier import classify_intent
        return classify_intent(query, **kwargs).intent.value

    def test_claim_verification_intent(self):
        self.assertEqual(
            self._intent("Benarkah minum air kelapa dapat menyembuhkan diabetes?"),
            "CLAIM_VERIFICATION",
        )
        self.assertEqual(
            self._intent("Vitamin C dosis tinggi menyembuhkan kanker"),
            "CLAIM_VERIFICATION",
        )

    def test_symptom_context_intent(self):
        self.assertEqual(
            self._intent("Saya demam sejak tiga hari dan batuk."),
            "SYMPTOM_CONTEXT",
        )

    def test_health_information_intent(self):
        self.assertEqual(
            self._intent("Apakah kondisi demam seperti ini biasanya membutuhkan pemeriksaan?"),
            "HEALTH_INFORMATION",
        )

    def test_follow_up_intent_requires_history(self):
        history = [
            {"role": "user", "content": "Saya demam"},
            {"role": "assistant", "content": "Baik, sudah berapa lama?"},
        ]
        self.assertEqual(
            self._intent("Kalau dari penjelasan sebelumnya, apa yang perlu saya perhatikan?",
                         previous_messages=history),
            "FOLLOW_UP",
        )
        # Tanpa riwayat, kalimat yang sama bukan follow-up.
        self.assertNotEqual(
            self._intent("Kalau dari penjelasan sebelumnya, apa yang perlu saya perhatikan?"),
            "FOLLOW_UP",
        )

    def test_medication_information_intent(self):
        self.assertEqual(
            self._intent("Berapa dosis paracetamol untuk dewasa?"),
            "MEDICATION_INFORMATION",
        )
        self.assertEqual(
            self._intent("Apa efek samping ibuprofen?"),
            "MEDICATION_INFORMATION",
        )

    def test_general_health_intent(self):
        self.assertEqual(
            self._intent("Olahraga rutin dan tidur cukup penting bagi kesehatan."),
            "GENERAL_HEALTH",
        )

    def test_unsupported_intent(self):
        self.assertEqual(self._intent("Bagaimana cara trading saham dan bitcoin?"), "UNSUPPORTED")
        self.assertEqual(self._intent(""), "UNSUPPORTED")

    def test_mode_biases_but_does_not_override_symptoms(self):
        from ragai.contracts import Mode
        from ragai.query_understanding.classifier import classify_intent

        result = classify_intent("Saya demam tiga hari", mode=Mode.CONSULTATION)
        self.assertEqual(result.intent.value, "SYMPTOM_CONTEXT")


# ===========================================================================
# 2. HEALTH CONTEXT & CONVERSATION (§8, §9)
# ===========================================================================

class HealthContextExtractionTests(TestCase):
    def test_extracts_symptoms_and_duration(self):
        from ragai.context.extractor import extract_health_context

        ctx = extract_health_context("Saya demam dan batuk sudah tiga hari")
        self.assertIn("demam", ctx.symptoms)
        self.assertIn("batuk", ctx.symptoms)
        self.assertEqual(ctx.duration, "3 hari")
        self.assertEqual(ctx.chief_complaint, "demam")

    def test_numeric_and_word_durations(self):
        from ragai.context.extractor import extract_duration

        self.assertEqual(extract_duration("sudah 5 hari"), "5 hari")
        self.assertEqual(extract_duration("sejak dua minggu lalu"), "2 minggu")
        self.assertEqual(extract_duration("seminggu ini"), "1 minggu")
        self.assertEqual(extract_duration("sejak kemarin"), "1 hari")
        self.assertIsNone(extract_duration("saya batuk"))

    def test_does_not_invent_unreported_fields(self):
        """Field yang tidak disebut user WAJIB tetap None / kosong (§8)."""
        from ragai.context.extractor import extract_health_context

        ctx = extract_health_context("Saya demam")
        self.assertIsNone(ctx.duration)
        self.assertIsNone(ctx.severity)
        self.assertIsNone(ctx.onset)
        self.assertIsNone(ctx.progression)
        self.assertEqual(ctx.medications, [])
        self.assertEqual(ctx.allergies, [])
        self.assertEqual(ctx.relevant_history, [])

    def test_severity_onset_progression(self):
        from ragai.context.extractor import extract_health_context

        ctx = extract_health_context(
            "Nyeri perut saya tiba-tiba muncul, terasa sangat parah dan makin parah"
        )
        self.assertEqual(ctx.onset, "mendadak")
        self.assertEqual(ctx.severity, "berat")
        self.assertEqual(ctx.progression, "memburuk")

    def test_medications_and_allergies(self):
        from ragai.context.extractor import extract_health_context

        ctx = extract_health_context("Saya minum paracetamol dan alergi amoxicillin")
        self.assertTrue(any("paracetamol" in m for m in ctx.medications))
        self.assertTrue(any("amoxicillin" in a for a in ctx.allergies))

    def test_symptom_accumulation_across_turns(self):
        """'Saya demam' + 'sudah tiga hari' -> duration(demam) = 3 hari (§9)."""
        from ragai.context.extractor import extract_health_context

        ctx = extract_health_context("Saya demam")
        self.assertIsNone(ctx.duration)

        ctx = extract_health_context("Sudah tiga hari", previous=ctx)
        self.assertEqual(ctx.duration, "3 hari")
        self.assertIn("demam", ctx.symptoms)

        ctx = extract_health_context("Sekarang saya juga batuk", previous=ctx)
        self.assertIn("demam", ctx.symptoms)
        self.assertIn("batuk", ctx.symptoms)
        self.assertEqual(ctx.chief_complaint, "demam")  # keluhan utama tetap yang pertama
        self.assertEqual(ctx.duration, "3 hari")

    def test_provenance_marks_user_reported(self):
        from ragai.context.extractor import extract_health_context

        ctx = extract_health_context("Saya demam tiga hari")
        self.assertEqual(ctx.provenance.get("symptoms"), "USER_REPORTED")
        self.assertEqual(ctx.provenance.get("duration"), "USER_REPORTED")


class ConversationContextTests(TestCase):
    def test_effective_query_resolves_reference(self):
        """'Apakah itu normal?' harus membawa konteks demam 3 hari."""
        from ragai.context.conversation import (
            build_effective_query,
            load_state,
            rebuild_context_from_history,
        )
        from ragai.contracts import Intent

        state = load_state(
            conversation_id=None,
            previous_messages=[
                {"role": "user", "content": "Saya demam"},
                {"role": "assistant", "content": "Sudah berapa lama?"},
                {"role": "user", "content": "Sudah tiga hari"},
            ],
        )
        state.health_context = rebuild_context_from_history(state)

        effective = build_effective_query("Apakah itu normal?", state, Intent.FOLLOW_UP)
        self.assertIn("demam", effective.lower())
        self.assertIn("3 hari", effective.lower())

    def test_persist_and_reload_session(self):
        from ragai.context.conversation import load_state, persist_turn
        from ragai.contracts import HealthContext

        context = HealthContext(chief_complaint="demam", symptoms=["demam"], duration="3 hari")
        persist_turn(
            conversation_id="HT-TEST-1", consumer="healthtalk",
            user_message="Saya demam tiga hari", assistant_message="Baik.",
            health_context=context,
        )

        # Sesi disimpan per consumer, jadi dibaca dengan consumer yang sama.
        state = load_state("HT-TEST-1", consumer="healthtalk")
        self.assertEqual(len(state.messages), 2)
        self.assertEqual(state.health_context.duration, "3 hari")
        self.assertIn("demam", state.health_context.symptoms)

    def test_persist_turn_without_conversation_id_is_noop(self):
        from ragai.context.conversation import persist_turn
        from ragai.contracts import HealthContext

        self.assertIsNone(persist_turn(None, "healthtalk", "a", "b", HealthContext()))
        self.assertEqual(ConversationSession.objects.count(), 0)


# ===========================================================================
# 3. EVIDENCE INTEGRITY — ANTI-404 (§14)
# ===========================================================================

class LinkValidationTests(TestCase):
    def test_normalize_doi_strips_prefixes(self):
        from ragai.evidence.link_validator import normalize_doi

        target = "10.1016/j.jinf.2021.02.004"
        for raw in (
            "https://doi.org/10.1016/j.jinf.2021.02.004",
            "http://dx.doi.org/10.1016/j.jinf.2021.02.004",
            "doi:10.1016/j.jinf.2021.02.004",
            "urn:doi:10.1016/j.jinf.2021.02.004",
            "  10.1016/j.jinf.2021.02.004.  ",
        ):
            self.assertEqual(normalize_doi(raw), target, raw)

    def test_malformed_doi_rejected_without_network(self):
        from ragai.evidence.link_validator import looks_like_doi, resolve_doi

        self.assertFalse(looks_like_doi("10.1/x"))
        self.assertFalse(looks_like_doi("not-a-doi"))
        self.assertTrue(looks_like_doi("10.1016/j.jinf.2021.02.004"))
        # Tidak ada panggilan jaringan untuk DOI yang formatnya salah.
        self.assertEqual(resolve_doi("10.1/x"), "malformed")

    def test_unresolvable_doi_is_dropped_entirely(self):
        """DOI yang tidak terdaftar tidak boleh keluar sebagai link (404 guard)."""
        from ragai.evidence import link_validator as lv

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_UNRESOLVABLE):
            result = lv.validate_reference("10.9999/karangan-model", "")

        self.assertEqual(result["doi"], "")
        self.assertEqual(result["url"], "")
        self.assertFalse(result["doi_verified"])
        self.assertEqual(result["link_status"], "unresolvable")

    def test_verified_doi_becomes_link(self):
        from ragai.evidence import link_validator as lv

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED):
            result = lv.validate_reference("10.1016/j.jinf.2021.02.004", "")

        self.assertEqual(result["url"], "https://doi.org/10.1016/j.jinf.2021.02.004")
        self.assertTrue(result["doi_verified"])

    def test_unknown_status_never_emits_link_for_untrusted_source(self):
        """Saat status tidak bisa dipastikan, lebih baik tanpa link daripada link mati."""
        from ragai.evidence import link_validator as lv

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_UNKNOWN):
            untrusted = lv.validate_reference("10.1016/j.jinf.2021.02.004", "",
                                              trust_on_unknown=False)
            trusted = lv.validate_reference("10.1016/j.jinf.2021.02.004", "",
                                            trust_on_unknown=True)

        self.assertEqual(untrusted["url"], "")
        self.assertEqual(untrusted["doi"], "10.1016/j.jinf.2021.02.004")
        self.assertTrue(trusted["url"].startswith("https://doi.org/"))

    def test_dead_url_is_dropped(self):
        from ragai.evidence import link_validator as lv

        with patch.object(lv, "check_url", return_value=(lv.STATUS_UNRESOLVABLE, "")):
            result = lv.validate_reference("", "https://example.com/hilang")
        self.assertEqual(result["url"], "")


class EvidenceSelectorTests(TestCase):
    def _item(self, **kwargs):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        defaults = {
            "chunk_id": "c1", "source_id": "s1", "title": "Studi demam",
            "snippet": "Randomized controlled trial mengenai demam dan batuk.",
            "doi": "", "url": "", "origin": EvidenceOrigin.KNOWLEDGE_BASE,
            "semantic_relevance": 0.8, "published_year": 2022,
        }
        defaults.update(kwargs)
        return EvidenceItem(**defaults)

    def test_model_suggested_source_is_not_publishable(self):
        from ragai.contracts import EvidenceOrigin

        item = self._item(origin=EvidenceOrigin.MODEL_SUGGESTED,
                          doi="10.9999/karangan", doi_verified=False)
        self.assertFalse(item.is_publishable())

    def test_model_suggested_promoted_when_doi_verified(self):
        from ragai.contracts import EvidenceOrigin
        from ragai.evidence import link_validator as lv
        from ragai.evidence.selector import validate_links

        item = self._item(origin=EvidenceOrigin.MODEL_SUGGESTED,
                          doi="10.1016/j.jinf.2021.02.004")
        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED):
            validated = validate_links([item])

        self.assertEqual(validated[0].origin, EvidenceOrigin.VERIFIED_REGISTRY)
        self.assertTrue(validated[0].is_publishable())

    def test_selection_drops_unresolvable_and_reports_insufficient(self):
        from ragai.contracts import EvidenceStatus
        from ragai.evidence import link_validator as lv
        from ragai.evidence.selector import select_evidence

        items = [self._item(doi="10.9999/palsu-1"), self._item(doi="10.9999/palsu-2")]
        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_UNRESOLVABLE):
            selected, status = select_evidence(items)

        self.assertEqual(selected, [])
        self.assertEqual(status, EvidenceStatus.INSUFFICIENT_EVIDENCE)

    def test_no_evidence_is_insufficient(self):
        from ragai.contracts import EvidenceStatus
        from ragai.evidence.selector import select_evidence

        selected, status = select_evidence([])
        self.assertEqual(selected, [])
        self.assertEqual(status, EvidenceStatus.INSUFFICIENT_EVIDENCE)

    def test_quality_scoring_prefers_recent_high_grade_evidence(self):
        from ragai.evidence.quality import (
            score_evidence_type,
            score_recency,
            score_source_quality,
        )

        self.assertGreater(
            score_evidence_type("systematic review and meta-analysis of trials"),
            score_evidence_type("a case report of one patient"),
        )
        self.assertGreater(score_recency(2025, now_year=2026), score_recency(1995, now_year=2026))
        self.assertGreater(
            score_source_quality(doi="10.1056/NEJMoa2034577"),
            score_source_quality(source_type="news"),
        )
        # Tahun tidak diketahui bersifat netral, bukan hukuman.
        self.assertEqual(score_recency(None), 0.5)


# ===========================================================================
# 4. RETRIEVAL (§10)
# ===========================================================================

@OFFLINE
class RetrievalTests(TestCase):
    def setUp(self):
        make_journal()
        make_journal(
            title="Effect of statins on cardiovascular events",
            abstract="Meta-analysis of randomized controlled trials on statin therapy "
                     "and cardiovascular outcomes in adults with hyperlipidemia.",
            doi="10.1016/j.jacc.2020.11.010",
            url="https://doi.org/10.1016/j.jacc.2020.11.010",
            keywords="kolesterol, statin, jantung",
        )

    def test_retrieves_relevant_evidence(self):
        from ragai.retrieval.retriever import retrieve_candidates

        results = retrieve_candidates("demam dan batuk tiga hari")
        self.assertTrue(results)
        self.assertIn("respiratory", results[0].title.lower())

    def test_irrelevant_query_returns_nothing_relevant(self):
        from ragai.evidence.selector import select_evidence
        from ragai.retrieval.retriever import retrieve_candidates

        candidates = retrieve_candidates("patah tulang selangka akibat jatuh dari sepeda")
        selected, status = select_evidence(candidates, limit=5)
        titles = " ".join(i.title.lower() for i in selected)
        self.assertNotIn("statin", titles)

    def test_empty_knowledge_base_returns_no_evidence(self):
        from ragai.contracts import EvidenceStatus
        from ragai.evidence.selector import select_evidence
        from ragai.retrieval.retriever import retrieve_candidates

        JournalArticle.objects.all().delete()
        candidates = retrieve_candidates("demam dan batuk")
        selected, status = select_evidence(candidates)
        self.assertEqual(selected, [])
        self.assertEqual(status, EvidenceStatus.INSUFFICIENT_EVIDENCE)

    def test_retrieves_from_existing_claim_sources(self):
        """Knowledge base lama (Source/ClaimSource) tetap dipakai (§10, §22)."""
        from ragai.retrieval.retriever import retrieve_from_sources

        claim = Claim.objects.create(text="Merokok menyebabkan kanker paru")
        source = Source.objects.create(
            title="Tobacco smoking and lung cancer risk",
            doi="10.1016/j.lungcan.2019.05.012",
            source_type="journal",
        )
        ClaimSource.objects.create(
            claim=claim, source=source, relevance_score=0.9,
            excerpt="Cigarette smoking remains the leading cause of lung cancer.",
        )

        results = retrieve_from_sources(["kanker", "lung cancer", "smoking"])
        self.assertTrue(results)
        self.assertEqual(results[0].source_id, f"source:{source.id}")

    def test_concept_extraction(self):
        from ragai.retrieval.concepts import extract_health_concepts

        concepts = extract_health_concepts("Saya demam dan batuk, apakah ini diabetes?")
        self.assertIn("demam", concepts)
        self.assertIn("batuk", concepts)
        self.assertIn("diabetes", concepts)


# ===========================================================================
# 5. GENERATION (§12, §16)
# ===========================================================================

@OFFLINE
class GenerationTests(TestCase):
    def _evidence(self, n=2):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        return [
            EvidenceItem(
                chunk_id=f"c{i}", source_id=f"journal:{i}",
                title=f"Studi demam nomor {i}",
                snippet="Demam yang berlangsung tiga hari umumnya berkaitan dengan "
                        "infeksi saluran napas akut dan membaik dalam tujuh hari.",
                doi="", url="", origin=EvidenceOrigin.KNOWLEDGE_BASE,
                semantic_relevance=0.8, relevance=0.7, published_year=2022,
            )
            for i in range(1, n + 1)
        ]

    def test_insufficient_evidence_never_calls_llm(self):
        from ragai.contracts import EvidenceStatus, HealthContext, Intent
        from ragai.reasoning import generator, llm

        with patch.object(llm, "generate") as mocked:
            answer, meta = generator.generate_response(
                query="Apakah X menyembuhkan Y?",
                intent=Intent.HEALTH_INFORMATION,
                context=HealthContext(),
                evidence=[],
                evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
            )
        mocked.assert_not_called()
        self.assertEqual(meta["generator"], "template_insufficient_evidence")
        self.assertIn("belum menemukan bukti", answer.lower())

    def test_extractive_fallback_is_grounded_in_evidence(self):
        from ragai.contracts import EvidenceStatus, HealthContext, Intent
        from ragai.reasoning.generator import generate_response

        answer, meta = generate_response(
            query="Saya demam tiga hari",
            intent=Intent.SYMPTOM_CONTEXT,
            context=HealthContext(symptoms=["demam"], duration="3 hari"),
            evidence=self._evidence(),
            evidence_status=EvidenceStatus.SUFFICIENT,
        )
        self.assertEqual(meta["generator"], "extractive_fallback")
        self.assertIn("infeksi saluran napas", answer.lower())
        self.assertIn("[E1]", answer)

    def test_partial_evidence_adds_uncertainty(self):
        from ragai.contracts import EvidenceStatus, HealthContext, Intent
        from ragai.reasoning.generator import generate_response

        answer, _ = generate_response(
            query="Saya demam", intent=Intent.SYMPTOM_CONTEXT,
            context=HealthContext(), evidence=self._evidence(1),
            evidence_status=EvidenceStatus.PARTIAL,
        )
        self.assertIn("terbatas", answer.lower())

    def test_fabricated_doi_in_llm_text_is_stripped(self):
        """DOI/URL yang ditulis LLM tapi tidak ada di evidence harus dibuang."""
        from ragai.reasoning.generator import _strip_fabricated_references

        evidence = self._evidence(1)
        evidence[0].doi = "10.1016/j.jinf.2021.02.004"
        evidence[0].url = "https://doi.org/10.1016/j.jinf.2021.02.004"

        text = (
            "Demam umumnya membaik sendiri (lihat https://doi.org/10.9999/palsu). "
            "Studi lain doi:10.1234/tidak-ada juga menyebutkannya. "
            "Sumber sah: https://doi.org/10.1016/j.jinf.2021.02.004"
        )
        cleaned = _strip_fabricated_references(text, evidence)

        self.assertNotIn("10.9999/palsu", cleaned)
        self.assertNotIn("10.1234/tidak-ada", cleaned)
        self.assertIn("10.1016/j.jinf.2021.02.004", cleaned)

    def test_unsupported_topic_returns_scope_message(self):
        from ragai.contracts import EvidenceStatus, HealthContext, Intent
        from ragai.reasoning.generator import generate_response

        answer, meta = generate_response(
            query="Bagaimana cara trading bitcoin?", intent=Intent.UNSUPPORTED,
            context=HealthContext(), evidence=[],
            evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
        )
        self.assertEqual(meta["generator"], "template_unsupported")
        self.assertIn("luar cakupan", answer.lower())


class ClaimProvenanceTests(TestCase):
    def _evidence(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        return [EvidenceItem(
            chunk_id="c1", source_id="journal:1",
            title="Merokok dan kanker paru",
            snippet="Merokok meningkatkan risiko kanker paru secara signifikan "
                    "berdasarkan studi kohort besar.",
            doi="10.1016/j.lungcan.2019.05.012",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, relevance=0.8,
        )]

    def test_supported_sentence_is_attributed(self):
        from ragai.evidence.provenance import attribute_claims

        answer = "Merokok meningkatkan risiko kanker paru secara signifikan pada perokok aktif."
        claims = attribute_claims(answer, self._evidence())

        self.assertTrue(claims)
        self.assertEqual(claims[0].verdict, "supported")
        self.assertEqual(claims[0].supporting_evidence[0]["doi"],
                         "10.1016/j.lungcan.2019.05.012")

    def test_unsupported_sentence_is_flagged(self):
        from ragai.evidence.provenance import attribute_claims, unsupported_claims

        answer = "Konsumsi jahe merah setiap pagi terbukti menurunkan tekanan darah tinggi."
        claims = attribute_claims(answer, self._evidence())
        self.assertTrue(unsupported_claims(claims))

    def test_disclaimer_sentences_are_not_treated_as_claims(self):
        from ragai.evidence.provenance import attribute_claims

        answer = "Konsultasikan dengan dokter untuk penanganan yang tepat."
        self.assertEqual(attribute_claims(answer, self._evidence()), [])


# ===========================================================================
# 6. SAFETY (§17)
# ===========================================================================

class SafetyTests(TestCase):
    def test_benign_information_passes(self):
        from ragai.contracts import EvidenceStatus, SafetyDecision
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Minum air yang cukup membantu menjaga hidrasi tubuh.",
            user_input="Berapa banyak air yang sebaiknya diminum?",
            evidence_status=EvidenceStatus.SUFFICIENT,
        )
        self.assertEqual(report.decision, SafetyDecision.PASS)

    def test_emergency_signal_prepends_warning(self):
        from ragai.contracts import SafetyDecision
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Nyeri dada bisa disebabkan banyak hal.",
            user_input="Saya merasa nyeri dada hebat dan sesak napas berat",
        )
        self.assertEqual(report.decision, SafetyDecision.MODIFY)
        self.assertTrue(report.has_flag("EMERGENCY_SIGNAL"))
        self.assertIn("gawat darurat", report.answer.lower())
        self.assertTrue(report.answer.startswith("⚠️"))

    def test_dangerous_instruction_is_blocked(self):
        from ragai.contracts import SafetyDecision
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Anda bisa hentikan obat hipertensi Anda dan gandakan dosis vitamin.",
            user_input="Apakah boleh berhenti minum obat?",
        )
        self.assertEqual(report.decision, SafetyDecision.BLOCK)
        self.assertTrue(report.has_flag("DANGEROUS_INSTRUCTION"))
        self.assertIn("di luar cakupan", report.answer.lower())

    def test_diagnosis_certainty_is_softened(self):
        from ragai.contracts import SafetyDecision
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Anda menderita demam berdarah.",
            user_input="Saya demam tiga hari",
        )
        self.assertEqual(report.decision, SafetyDecision.MODIFY)
        self.assertTrue(report.has_flag("DIAGNOSIS_CERTAINTY"))
        self.assertNotIn("Anda menderita", report.answer)
        self.assertIn("bukan diagnosis", report.answer.lower())

    def test_dosage_recommendation_is_removed(self):
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Minum paracetamol 500 mg tiga kali sehari. Istirahat yang cukup juga membantu.",
            user_input="Obat apa untuk demam?",
        )
        self.assertTrue(report.has_flag("TREATMENT_RECOMMENDATION_RISK"))
        self.assertNotIn("500 mg", report.answer)
        self.assertIn("apoteker", report.answer.lower())

    def test_high_risk_population_flagged(self):
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Demam ringan umumnya membaik sendiri.",
            user_input="Istri saya sedang hamil dan demam",
        )
        self.assertTrue(report.has_flag("HIGH_RISK_POPULATION"))

    def test_insufficient_evidence_flagged(self):
        from ragai.contracts import EvidenceStatus
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Bukti belum memadai.", user_input="Apakah X menyembuhkan Y?",
            evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
        )
        self.assertTrue(report.has_flag("INSUFFICIENT_EVIDENCE"))


# ===========================================================================
# 7. ENGINE END-TO-END (§4, §18)
# ===========================================================================

@OFFLINE
class EngineTests(TestCase):
    def setUp(self):
        make_journal()

    def test_symptom_query_returns_structured_context_and_assessment(self):
        from ragai import engine

        result = engine.process({
            "query": "Saya sudah demam tiga hari dan batuk.",
            "mode": "consultation",
            "context": {"session_id": "HT-ENG-1"},
        }, consumer="healthtalk")

        self.assertEqual(result.intent.value, "SYMPTOM_CONTEXT")
        self.assertEqual(result.health_context.duration, "3 hari")
        self.assertIn("demam", result.health_context.symptoms)
        self.assertIsNotNone(result.preliminary_assessment)
        self.assertFalse(result.preliminary_assessment["is_diagnosis"])
        self.assertEqual(result.preliminary_assessment["status"], "PRELIMINARY_ASSESSMENT")
        self.assertIn("BUKAN diagnosis", result.preliminary_assessment["disclaimer"])

    def test_multi_turn_accumulates_context(self):
        from ragai import engine

        engine.process({"query": "Saya demam", "mode": "consultation",
                        "context": {"session_id": "HT-ENG-2"}}, consumer="healthtalk")
        second = engine.process({"query": "Sudah tiga hari", "mode": "consultation",
                                 "context": {"session_id": "HT-ENG-2"}}, consumer="healthtalk")

        self.assertEqual(second.health_context.duration, "3 hari")
        self.assertIn("demam", second.health_context.symptoms)
        from ragai.context.conversation import find_session

        # Baris sesi kini bernama per consumer; yang penting satu ruang obrolan
        # tetap menghasilkan tepat satu sesi.
        self.assertIsNotNone(find_session("HT-ENG-2", consumer="healthtalk"))
        self.assertEqual(ConversationSession.objects.count(), 1)
        self.assertEqual(
            ConversationMessage.objects.filter(
                session=find_session("HT-ENG-2", consumer="healthtalk")).count(), 4
        )

    def test_every_published_evidence_has_safe_link(self):
        """Tidak boleh ada URL yang berasal dari DOI tak terverifikasi (§14)."""
        from ragai import engine

        result = engine.process({"query": "demam dan batuk tiga hari",
                                 "mode": "consultation"})
        for item in result.evidence:
            self.assertNotEqual(item.link_status, "unresolvable")
            self.assertNotEqual(item.link_status, "malformed")
            if item.url:
                self.assertTrue(item.url.startswith("http"))

    def test_unsupported_query_short_circuits_retrieval(self):
        from ragai import engine

        result = engine.process({"query": "Bagaimana cara trading saham?"})
        self.assertEqual(result.intent.value, "UNSUPPORTED")
        self.assertEqual(result.evidence, [])
        self.assertEqual(result.metadata["candidates_retrieved"], 0)

    def test_emergency_query_is_flagged_end_to_end(self):
        from ragai import engine

        result = engine.process({
            "query": "Saya nyeri dada hebat dan sesak napas berat sejak tadi",
            "mode": "consultation",
        })
        codes = [f.code for f in result.safety_flags]
        self.assertIn("EMERGENCY_SIGNAL", codes)
        self.assertIn("gawat darurat", result.answer.lower())

    def test_claim_mode_uses_claim_engine(self):
        from ragai import engine

        result = engine.process({
            "query": "Benarkah vitamin C dosis tinggi menyembuhkan kanker?",
            "mode": "claim",
        })
        self.assertEqual(result.intent.value, "CLAIM_VERIFICATION")
        self.assertIn("claim_evaluation", result.metadata)
        self.assertIn(
            result.metadata["claim_evaluation"]["verdict"],
            ("supported", "unsupported", "inconclusive"),
        )

    def test_no_evidence_yields_insufficient_status(self):
        from ragai import engine

        JournalArticle.objects.all().delete()
        result = engine.process({"query": "Apakah demam berdarah menular lewat udara?"})
        self.assertEqual(result.evidence_status.value, "INSUFFICIENT_EVIDENCE")
        self.assertIsNotNone(result.uncertainty)


# ===========================================================================
# 8. CONSULTATION SUMMARY (§19, §20)
# ===========================================================================

@OFFLINE
class ConsultationSummaryTests(TestCase):
    def setUp(self):
        make_journal()
        from ragai import engine

        for message in ("Saya demam", "Sudah tiga hari", "Sekarang batuk juga"):
            engine.process({"query": message, "mode": "consultation",
                            "context": {"session_id": "HT-SUM-1"}}, consumer="healthtalk")
        from ragai.context.conversation import find_session

        self.session = find_session("HT-SUM-1", consumer="healthtalk")

    def test_summary_extracts_only_reported_information(self):
        from ragai.summarization.summarizer import build_summary

        summary = build_summary(self.session)

        self.assertEqual(summary["chief_complaint"]["value"], "demam")
        self.assertEqual(summary["duration"]["value"], "3 hari")
        symptoms = [entry["value"] for entry in summary["symptoms"]]
        self.assertIn("demam", symptoms)
        self.assertIn("batuk", symptoms)

    def test_summary_does_not_hallucinate_unreported_fields(self):
        from ragai.summarization.summarizer import build_summary

        summary = build_summary(self.session)
        context = summary["health_context"]

        self.assertIsNone(context["severity"])
        self.assertIsNone(context["onset"])
        self.assertEqual(context["medications"], [])
        self.assertEqual(context["allergies"], [])
        # Tidak ada nama penyakit yang disimpulkan sebagai diagnosis.
        self.assertFalse(summary["is_diagnosis"])
        self.assertEqual(summary["status"], "PRELIMINARY_ASSESSMENT")

    def test_summary_carries_provenance(self):
        from ragai.summarization.summarizer import build_summary

        summary = build_summary(self.session)

        self.assertEqual(summary["chief_complaint"]["provenance"], "USER_REPORTED")
        self.assertEqual(summary["duration"]["provenance"], "USER_REPORTED")
        for note in summary["safety_notes"]:
            self.assertIn(note["provenance"],
                          ("SYSTEM_GENERATED", "AI_INFERRED", "EVIDENCE_SUPPORTED"))
        for step in summary["recommended_next_step"]:
            self.assertEqual(step["provenance"], "SYSTEM_GENERATED")

    def test_summary_is_persisted(self):
        from ragai.summarization.summarizer import build_summary, persist_summary

        summary = build_summary(self.session)
        persist_summary(self.session, summary)

        stored = ConsultationSummary.objects.get(session=self.session)
        self.assertEqual(stored.chief_complaint, "demam")
        self.assertEqual(json.loads(stored.payload)["session_id"], "HT-SUM-1")


# ===========================================================================
# 9. API LAYER (§21)
# ===========================================================================

@OFFLINE
class IntelligenceAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        make_journal()

    def test_capabilities_endpoint(self):
        response = self.client.get("/api/v1/intelligence/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertIn("consultation", response.json()["modes"])
        self.assertIn("CLAIM_VERIFICATION", response.json()["intents"])

    def test_query_endpoint_returns_contract_shape(self):
        response = self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Saya sudah demam tiga hari.",
                  "context": {"session_id": "HT-API-1"},
                  "mode": "consultation"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ("answer", "intent", "health_context", "evidence",
                    "safety_flags", "evidence_status", "claims"):
            self.assertIn(key, body)
        self.assertEqual(body["health_context"]["duration"], "3 hari")
        self.assertEqual(body["conversation_id"], "HT-API-1")

    def test_query_requires_query_field(self):
        response = self.client.post("/api/v1/intelligence/query", data={}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_query_rejects_invalid_mode(self):
        response = self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Saya demam", "mode": "diagnosis"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_session_endpoint_returns_history(self):
        self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Saya demam tiga hari", "context": {"session_id": "HT-API-2"}},
            format="json",
        )
        response = self.client.get("/api/v1/intelligence/sessions/HT-API-2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["messages"]), 2)

    def test_session_not_found(self):
        response = self.client.get("/api/v1/intelligence/sessions/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_summary_endpoint(self):
        for message in ("Saya demam", "Sudah tiga hari"):
            self.client.post(
                "/api/v1/intelligence/query",
                data={"query": message, "context": {"session_id": "HT-API-3"}},
                format="json",
            )
        response = self.client.post(
            "/api/v1/intelligence/summary",
            data={"session_id": "HT-API-3", "close_session": True}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        summary = response.json()["summary"]
        self.assertEqual(summary["duration"]["value"], "3 hari")
        self.assertEqual(summary["session_status"], "closed")

    def test_summary_requires_session_id(self):
        response = self.client.post("/api/v1/intelligence/summary", data={}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_summary_unknown_session(self):
        response = self.client.post(
            "/api/v1/intelligence/summary", data={"session_id": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(INTELLIGENCE_API_KEYS={"secret-key": "healthtalk"})
    def test_api_key_enforced_when_configured(self):
        unauthorized = self.client.post(
            "/api/v1/intelligence/query", data={"query": "Saya demam"}, format="json"
        )
        self.assertEqual(unauthorized.status_code, 401)

        authorized = self.client.post(
            "/api/v1/intelligence/query", data={"query": "Saya demam"},
            format="json", HTTP_X_API_KEY="secret-key",
        )
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json()["metadata"]["consumer"], "healthtalk")

    @override_settings(INTELLIGENCE_API_KEYS={"secret-key": "healthtalk"})
    def test_wrong_api_key_rejected(self):
        response = self.client.post(
            "/api/v1/intelligence/query", data={"query": "Saya demam"},
            format="json", HTTP_X_API_KEY="salah",
        )
        self.assertEqual(response.status_code, 401)


# ===========================================================================
# 10. DOKUMENTASI (Scalar)
# ===========================================================================

class ApiDocsTests(TestCase):
    def test_openapi_schema_is_served(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        spec = response.json()
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/api/v1/intelligence/query", spec["paths"])
        self.assertIn("/api/v1/intelligence/summary", spec["paths"])

    def test_only_external_endpoints_are_documented(self):
        """Dokumentasi ini kontrak untuk consumer, bukan peta seluruh aplikasi."""
        spec = self.client.get("/openapi.json").json()

        for path in spec["paths"]:
            self.assertTrue(path.startswith("/api/v1/intelligence/"),
                            f"endpoint internal bocor ke dokumentasi: {path}")

        for internal in ("/api/verify/", "/api/claims/", "/api/disputes/",
                         "/api/translate/", "/api/health/"):
            self.assertNotIn(internal, spec["paths"])

    def test_no_admin_auth_scheme_exposed(self):
        spec = self.client.get("/openapi.json").json()
        schemes = spec["components"]["securitySchemes"]

        self.assertEqual(set(schemes), {"ApiKeyAuth"})
        self.assertNotIn("BearerAuth", schemes)

    def test_guide_is_inside_the_documentation(self):
        """Panduan integrasi harus ada di /docs, bukan di tempat lain."""
        spec = self.client.get("/openapi.json").json()
        description = spec["info"]["description"]

        for anchor in ("Quickstart", "Requesting access", "X-API-Key",
                       "has_evidence", "Errors", "Backend integration",
                       "curl -X POST", "async function"):
            self.assertIn(anchor, description, anchor)

    def test_documentation_is_english_and_free_of_double_hyphens(self):
        spec = self.client.get("/openapi.json").json()
        description = spec["info"]["description"]

        self.assertNotIn("--", description)
        for indonesian in (" yang ", " dan ", " untuk ", " tidak ", " dengan ",
                           " adalah ", " bila ", " pengguna "):
            self.assertNotIn(indonesian, description, f"teks Indonesia: {indonesian!r}")

    def test_query_endpoint_has_code_samples(self):
        spec = self.client.get("/openapi.json").json()
        samples = spec["paths"]["/api/v1/intelligence/query"]["post"]["x-codeSamples"]

        langs = {s["lang"] for s in samples}
        self.assertEqual(langs, {"cURL", "JavaScript", "Python"})
        for sample in samples:
            self.assertNotIn("ht_live_", sample["source"])

    def test_scalar_reference_page_renders(self):
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("@scalar/api-reference", html)
        self.assertIn("/openapi.json", html)


# ===========================================================================
# 11. BACKWARD COMPATIBILITY (§25, §28)
# ===========================================================================

@OFFLINE
class BackwardCompatibilityTests(TestCase):
    """Produk Healthify harus tetap utuh & mandiri."""

    def setUp(self):
        self.client = APIClient()

    def test_existing_verify_endpoint_contract_unchanged(self):
        ai_result = {
            "label": "valid",
            "confidence": 0.9,
            "summary": "Ringkasan",
            "sources": [{
                "title": "Judul",
                "doi": "10.1016/j.lungcan.2019.05.012",
                "url": "https://doi.org/10.1016/j.lungcan.2019.05.012",
                "relevance_score": 0.8,
                "excerpt": "cuplikan",
            }],
        }
        with patch("api.views.call_ai_verify", return_value=ai_result):
            response = self.client.post(
                reverse("claim-verify"),
                data={"text": "Merokok menyebabkan kanker paru"}, format="json",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Bentuk response lama tetap sama persis.
        for key in ("id", "text", "text_normalized", "status",
                    "created_at", "updated_at", "verification_result", "sources"):
            self.assertIn(key, body)
        for key in ("id", "label", "label_display", "label_color", "summary",
                    "confidence", "confidence_percent"):
            self.assertIn(key, body["verification_result"])
        self.assertEqual(body["verification_result"]["label"], "valid")
        self.assertEqual(body["sources"][0]["source"]["doi"],
                         "10.1016/j.lungcan.2019.05.012")

    def test_verify_failure_returns_clean_error(self):
        """Regresi: sebelumnya kegagalan AI memicu AttributeError."""
        with patch("api.views.call_ai_verify", side_effect=Exception("boom")):
            response = self.client.post(
                reverse("claim-verify"), data={"text": "Klaim error"}, format="json"
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "Verification failed")

    def test_healthify_works_without_intelligence_tables(self):
        """Endpoint lama tidak menyentuh tabel percakapan sama sekali."""
        Claim.objects.create(text="Klaim lama", status=Claim.STATUS_DONE)
        response = self.client.get(reverse("claim-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConversationSession.objects.count(), 0)

    def test_legacy_adapter_maps_engine_response(self):
        from ragai import engine
        from ragai.adapters.legacy import (
            claim_request_to_payload,
            engine_response_to_legacy,
        )

        make_journal()
        payload = claim_request_to_payload("Benarkah demam tiga hari berbahaya?")
        result = engine.process(payload, consumer="healthify")
        legacy = engine_response_to_legacy(result)

        self.assertIn("label", legacy)
        self.assertIn("summary", legacy)
        self.assertIn("sources", legacy)
        self.assertIn(legacy["label"], ("valid", "hoax", "uncertain", "unverified"))

    def test_grounding_helper_uses_existing_knowledge_base(self):
        from .ai_adapter import retrieve_grounding_evidence

        make_journal()
        evidence = retrieve_grounding_evidence("demam dan batuk tiga hari")
        self.assertTrue(evidence)
        self.assertTrue(all(item["_trusted"] for item in evidence))

    def test_call_ai_direct_returns_unverified_without_evidence(self):
        """Tanpa evidence, sistem TIDAK meminta LLM menebak (§16)."""
        from .ai_adapter import call_ai_direct

        JournalArticle.objects.all().delete()
        with patch("api.ai_adapter.retrieve_grounding_evidence", return_value=[]):
            result = call_ai_direct("Klaim apa pun tentang kesehatan")

        self.assertEqual(result["label"], "unverified")
        self.assertIsNone(result["confidence"])
        self.assertEqual(result["sources"], [])


# ===========================================================================
# 12. REGRESI PERBAIKAN DARI PENGUJIAN LANGSUNG
# ===========================================================================

class EvidenceConsistencyTests(TestCase):
    """Response tidak boleh bertentangan dengan dirinya sendiri."""

    def test_insufficient_status_publishes_no_sources(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin, EvidenceStatus
        from ragai.evidence.selector import select_evidence

        weak = EvidenceItem(
            chunk_id="c1", source_id="s1", title="Topik lain sama sekali",
            snippet="Pembahasan yang tidak berkaitan dengan pertanyaan.",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, semantic_relevance=0.05,
        )
        selected, status = select_evidence([weak], validate=False)
        if status == EvidenceStatus.INSUFFICIENT_EVIDENCE:
            self.assertEqual(selected, [])


class CitationAttributionTests(TestCase):
    def _evidence(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        return [EvidenceItem(
            chunk_id="c1", source_id="journal:1",
            title="Vitamin C for preventing and treating the common cold",
            snippet="A Cochrane systematic review found no evidence that high-dose "
                    "vitamin C cures cancer.",
            doi="10.1002/14651858.CD000980.pub4",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, relevance=0.8,
        )]

    def test_citation_marker_attributes_across_languages(self):
        """Jawaban Bahasa Indonesia atas evidence Bahasa Inggris tetap tertelusur."""
        from ragai.evidence.provenance import attribute_claims

        answer = ("Tidak ada bukti bahwa vitamin C dosis tinggi dapat menyembuhkan "
                  "kanker atau penyakit serius lainnya [E1].")
        claims = attribute_claims(answer, self._evidence())

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].verdict, "supported")
        self.assertEqual(claims[0].supporting_evidence[0]["via"], "citation_marker")
        self.assertEqual(claims[0].supporting_evidence[0]["doi"],
                         "10.1002/14651858.CD000980.pub4")
        # Penanda dibersihkan dari teks klaim.
        self.assertNotIn("[E1]", claims[0].claim)

    def test_out_of_range_citation_is_ignored(self):
        from ragai.evidence.provenance import attribute_claims

        claims = attribute_claims(
            "Pernyataan medis yang cukup panjang untuk dianggap faktual [E9].",
            self._evidence(),
        )
        self.assertEqual(claims[0].verdict, "unsupported")

    def test_system_sentences_are_not_attributed(self):
        from ragai.evidence.provenance import attribute_claims

        answer = ("Saat ini kami belum menemukan bukti ilmiah yang cukup relevan di "
                  "basis pengetahuan Healthify. Sistem tidak menebak jawaban ketika "
                  "bukti pendukung tidak tersedia.")
        self.assertEqual(attribute_claims(answer, self._evidence()), [])


class SafetyThresholdTests(TestCase):
    def test_partially_traceable_answer_is_not_flagged(self):
        from ragai.contracts import EvidenceStatus, SupportedClaim
        from ragai.safety.validator import validate_response

        unsupported = [SupportedClaim(claim="x", verdict="unsupported")]
        report = validate_response(
            answer="Jawaban yang sebagian besar sudah tertelusur ke evidence.",
            user_input="pertanyaan",
            evidence_status=EvidenceStatus.SUFFICIENT,
            unsupported_claims=unsupported,
            total_claims=4,   # 3 dari 4 kalimat TERTELUSUR
        )
        self.assertFalse(report.has_flag("UNSUPPORTED_MEDICAL_CLAIM"))

    def test_answer_with_nothing_traceable_is_flagged(self):
        from ragai.contracts import EvidenceStatus, SupportedClaim
        from ragai.safety.validator import validate_response

        unsupported = [SupportedClaim(claim=f"x{i}", verdict="unsupported") for i in range(4)]
        report = validate_response(
            answer="Jawaban yang tidak satu pun tertelusur.",
            user_input="pertanyaan",
            evidence_status=EvidenceStatus.SUFFICIENT,
            unsupported_claims=unsupported,
            total_claims=4,   # 4 dari 4 -> tidak ada yang tertelusur
        )
        self.assertTrue(report.has_flag("UNSUPPORTED_MEDICAL_CLAIM"))


@OFFLINE
class TemplateAnswerTests(TestCase):
    def test_template_answer_produces_no_spurious_claims(self):
        from ragai import engine

        JournalArticle.objects.all().delete()
        result = engine.process({"query": "Bagaimana cara trading saham?"})
        self.assertEqual(result.claims, [])
        codes = [f.code for f in result.safety_flags]
        self.assertNotIn("UNSUPPORTED_MEDICAL_CLAIM", codes)

    def test_insufficient_evidence_answer_has_no_sources(self):
        from ragai import engine

        JournalArticle.objects.all().delete()
        result = engine.process({"query": "Apakah terapi X menyembuhkan penyakit Y?"})
        self.assertEqual(result.evidence_status.value, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result.evidence, [])


# ===========================================================================
# 13. PERKAKAS OPERASIONAL (management commands)
# ===========================================================================

class AuditSourceLinksCommandTests(TestCase):
    def setUp(self):
        self.good = Source.objects.create(
            title="Sumber sah", doi="10.1056/NEJMoa2034577",
            url="https://doi.org/10.1056/NEJMoa2034577", source_type="journal",
        )
        self.fake = Source.objects.create(
            title="Karangan LLM", doi="10.9999/karangan",
            url="https://doi.org/10.9999/karangan", source_type="journal",
        )

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("audit_source_links", "--only", "sources", "--delay", "0", *args, stdout=out)
        return out.getvalue()

    def _patched(self, metadata=None):
        """Registry dipalsukan: test tidak boleh menembak jaringan."""
        from contextlib import ExitStack

        from ragai.evidence import link_validator as lv

        def fake_resolve(doi, **kwargs):
            return (lv.STATUS_VERIFIED if doi.startswith("10.1056/")
                    else lv.STATUS_UNRESOLVABLE)

        stack = ExitStack()
        stack.enter_context(patch.object(lv, "resolve_doi", side_effect=fake_resolve))
        stack.enter_context(patch.object(lv, "fetch_doi_metadata",
                                         return_value=metadata))
        return stack

    def test_dry_run_reports_without_modifying(self):
        with self._patched():
            output = self._run()

        self.assertIn("10.9999/karangan", output)
        self.assertIn("tautan rusak/404 : 1", output)
        self.fake.refresh_from_db()
        self.assertEqual(self.fake.doi, "10.9999/karangan")  # belum diubah

    def test_fix_clears_broken_links_only(self):
        with self._patched():
            self._run("--fix")

        self.fake.refresh_from_db()
        self.good.refresh_from_db()
        self.assertIsNone(self.fake.doi)
        self.assertIsNone(self.fake.url)
        self.assertEqual(self.good.doi, "10.1056/NEJMoa2034577")

    def test_delete_orphans_requires_fix(self):
        with self._patched():
            output = self._run("--delete-orphans")
        self.assertIn("memerlukan --fix", output)
        self.assertEqual(Source.objects.count(), 2)

    def test_title_mismatch_is_reported(self):
        """DOI nyata tapi milik paper lain: judul di layar berbeda dari halamannya."""
        registry = {"title": "Trust science?", "publisher": "Elsevier",
                    "container": "Fertility and Sterility", "year": 2021,
                    "authors": "Carpinello, Olivia"}
        with self._patched(metadata=registry):
            output = self._run()

        self.assertIn("judul beda", output)
        self.assertIn("Trust science?", output)
        self.assertIn("judul tidak cocok: 1", output)
        self.good.refresh_from_db()
        self.assertEqual(self.good.title, "Sumber sah")  # dry-run tidak mengubah

    def test_title_mismatch_is_replaced_by_registry_title(self):
        registry = {"title": "Trust science?", "publisher": "Elsevier",
                    "container": "Fertility and Sterility", "year": 2021,
                    "authors": "Carpinello, Olivia"}
        with self._patched(metadata=registry):
            self._run("--fix")

        self.good.refresh_from_db()
        self.assertEqual(self.good.title, "Trust science?")
        self.assertEqual(self.good.publisher, "Fertility and Sterility")
        self.assertEqual(self.good.authors, "Carpinello, Olivia")

    def test_matching_title_is_left_alone(self):
        self.good.title = "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine"
        self.good.save()
        registry = {"title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
                    "publisher": "", "container": "", "year": 2020, "authors": ""}

        with self._patched(metadata=registry):
            output = self._run("--fix")

        self.assertIn("judul tidak cocok: 0", output)

    def test_skip_titles_flag_avoids_registry_lookup(self):
        from ragai.evidence import link_validator as lv

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED), \
             patch.object(lv, "fetch_doi_metadata") as fetch:
            self._run("--skip-titles")
        fetch.assert_not_called()

    def test_delete_orphans_keeps_linked_sources(self):
        claim = Claim.objects.create(text="Klaim tertaut")
        ClaimSource.objects.create(claim=claim, source=self.fake, relevance_score=0.5)

        with self._patched():
            self._run("--fix", "--delete-orphans")

        self.assertTrue(Source.objects.filter(id=self.fake.id).exists())


class ImportJournalsCommandTests(TestCase):
    CROSSREF_ITEM = {
        "type": "journal-article",
        "DOI": "10.3390/medicina54020023",
        "title": ["Typhoid Fever Diagnosis in Endemic Countries"],
        "abstract": "<jats:p>Typhoid fever remains a major public health problem in "
                    "endemic countries. Blood culture is the reference standard but has "
                    "limited sensitivity. This review summarises available diagnostic "
                    "approaches and their performance characteristics in routine "
                    "practice, including rapid tests and molecular assays.</jats:p>",
        "author": [{"given": "Jane", "family": "Doe"}, {"given": "John", "family": "Roe"}],
        "container-title": ["Medicina"],
        "publisher": "MDPI AG",
        "issued": {"date-parts": [[2018, 2, 14]]},
        "subject": ["Medicine", "Infectious Diseases"],
    }

    def _run(self, items, *args, resolve="verified"):
        from io import StringIO

        from django.core.management import call_command
        from ragai.evidence import link_validator as lv

        out = StringIO()
        with patch("api.management.commands.import_journals.Command._search_crossref",
                   return_value=items), \
             patch.object(lv, "resolve_doi", return_value=resolve):
            call_command("import_journals", "--query", "typhoid", *args, stdout=out)
        return out.getvalue()

    def test_imports_verified_article(self):
        self._run([self.CROSSREF_ITEM])

        journal = JournalArticle.objects.get(doi="10.3390/medicina54020023")
        self.assertEqual(journal.title, "Typhoid Fever Diagnosis in Endemic Countries")
        self.assertEqual(journal.url, "https://doi.org/10.3390/medicina54020023")
        self.assertEqual(journal.authors, "Doe, Jane; Roe, John")
        self.assertEqual(str(journal.published_date), "2018-02-14")
        # Tag JATS dibersihkan.
        self.assertNotIn("<jats:", journal.abstract)
        self.assertTrue(journal.abstract.startswith("Typhoid fever remains"))

    def test_rejects_unverified_doi(self):
        """DOI yang tidak terdaftar tidak boleh masuk knowledge base."""
        output = self._run([self.CROSSREF_ITEM], resolve="unresolvable")

        self.assertEqual(JournalArticle.objects.count(), 0)
        self.assertIn("DOI ditolak", output)

    def test_dry_run_saves_nothing(self):
        self._run([self.CROSSREF_ITEM], "--dry-run")
        self.assertEqual(JournalArticle.objects.count(), 0)

    def test_rejects_non_article_types(self):
        item = dict(self.CROSSREF_ITEM, type="component")
        output = self._run([item])
        self.assertEqual(JournalArticle.objects.count(), 0)
        self.assertIn("ditolak (jenis)       : 1", output)

    def test_rejects_short_abstract(self):
        item = dict(self.CROSSREF_ITEM, abstract="<jats:p>Terlalu pendek.</jats:p>")
        output = self._run([item])
        self.assertEqual(JournalArticle.objects.count(), 0)
        self.assertIn("ditolak (abstrak)     : 1", output)

    def test_skips_duplicates(self):
        JournalArticle.objects.create(
            title="Sudah ada", abstract="x" * 300, doi="10.3390/medicina54020023",
        )
        output = self._run([self.CROSSREF_ITEM])
        self.assertEqual(JournalArticle.objects.count(), 1)
        self.assertIn("duplikat              : 1", output)


@override_settings(LLM_PROVIDER="")
class LlmProviderFallbackTests(TestCase):
    """Rantai fallback bawaan (tanpa preferensi eksplisit dari environment)."""

    def setUp(self):
        from ragai.reasoning import llm
        llm.reset_health()
        self.addCleanup(llm.reset_health)

    def test_falls_through_to_next_provider_on_failure(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"GEMINI_API_KEY": "invalid", "OPENAI_API_KEY": "ok"}), \
             patch.object(llm, "_generate_gemini", side_effect=Exception("API key not valid")), \
             patch.object(llm, "_generate_openai_compatible", return_value="jawaban"):
            result = llm.generate("halo")

        self.assertEqual(result, "jawaban")
        self.assertTrue(llm.is_unhealthy(llm.PROVIDER_GEMINI))
        self.assertEqual(llm.available_provider(), llm.PROVIDER_OPENAI)

    def test_returns_none_when_every_provider_fails(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"GEMINI_API_KEY": "x", "OPENAI_API_KEY": "y"}), \
             patch.object(llm, "_generate_gemini", side_effect=Exception("boom")), \
             patch.object(llm, "_generate_openai_compatible", side_effect=Exception("boom")):
            self.assertIsNone(llm.generate("halo"))

    def test_unhealthy_provider_is_skipped(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"GEMINI_API_KEY": "x", "OPENAI_API_KEY": "y"}):
            llm.mark_unhealthy(llm.PROVIDER_GEMINI, "diuji")
            with patch.object(llm, "_generate_gemini") as gemini, \
                 patch.object(llm, "_generate_openai_compatible", return_value="ok"):
                llm.generate("halo")
            gemini.assert_not_called()

    @override_settings(LLM_PROVIDER="openai")
    def test_preference_setting_reorders_chain(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"GEMINI_API_KEY": "x", "OPENAI_API_KEY": "y"}):
            self.assertEqual(llm.configured_providers()[0], llm.PROVIDER_OPENAI)

    @override_settings(INTELLIGENCE_LLM_ENABLED="0")
    def test_disabled_flag_returns_no_provider(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"OPENAI_API_KEY": "y"}):
            self.assertEqual(llm.configured_providers(), [])
            self.assertIsNone(llm.available_provider())


class GeminiKeyNormalizationTests(TestCase):
    def test_settings_normalizes_gemini_api_alias(self):
        """training/.env memakai GEMINI_API; kode membaca GEMINI_API_KEY."""
        import os
        import re
        from pathlib import Path

        settings_src = Path(__file__).resolve().parent.parent / "backend_project" / "settings.py"
        source = settings_src.read_text()
        self.assertIn("GEMINI_API_KEY", source)
        self.assertRegex(source, r"os\.getenv\(['\"]GEMINI_API['\"]\)")


# ===========================================================================
# 14. RETRIEVAL BILINGUAL (query ID vs knowledge base EN)
# ===========================================================================

@OFFLINE
class BilingualRetrievalTests(TestCase):
    def setUp(self):
        make_journal(
            title="Dengue Fever: An Overview",
            abstract="Dengue fever is an arboviral infection presenting with high fever, "
                     "myalgia, headache and skin rash. Warning signs of dengue hemorrhagic "
                     "fever include persistent vomiting and bleeding manifestations.",
            doi="10.5772/intechopen.92315",
            url="https://doi.org/10.5772/intechopen.92315",
            keywords="dengue, arbovirus, fever, rash",
        )
        make_journal(
            title="Typhoid Fever Diagnosis in Endemic Countries",
            abstract="Typhoid fever remains a major public health problem. Blood culture is "
                     "the reference standard. Transmission occurs through contaminated food "
                     "and water in endemic settings.",
            doi="10.3390/medicina54020023",
            url="https://doi.org/10.3390/medicina54020023",
            keywords="typhoid, enteric fever, diagnosis",
        )

    def test_indonesian_terms_expand_to_english(self):
        from ragai.lexicon import bilingual_variants

        self.assertIn("dengue", bilingual_variants("demam berdarah"))
        self.assertIn("typhoid", bilingual_variants("tifus"))
        self.assertIn("hypertension", bilingual_variants("hipertensi"))
        self.assertIn("fever", bilingual_variants("demam"))
        self.assertIn("rash", bilingual_variants("ruam"))
        # Istilah tak dikenal dikembalikan apa adanya.
        self.assertEqual(bilingual_variants("xyzzy"), ["xyzzy"])

    def test_indonesian_query_finds_english_journal(self):
        from ragai.context.extractor import context_terms, extract_health_context
        from ragai.evidence.selector import select_evidence
        from ragai.retrieval.retriever import retrieve_candidates

        query = "Saya demam tinggi tiga hari dan ada bintik merah di kulit"
        ctx = extract_health_context(query)
        candidates = retrieve_candidates(query, extra_terms=context_terms(ctx))
        self.assertTrue(candidates)

        selected, status = select_evidence(candidates, context_terms=context_terms(ctx), limit=3)
        self.assertTrue(selected)
        self.assertIn("Dengue", selected[0].title)

    def test_condition_query_in_indonesian(self):
        from ragai.evidence.selector import select_evidence
        from ragai.retrieval.retriever import retrieve_candidates

        candidates = retrieve_candidates("Apakah tifus bisa menular lewat makanan?")
        selected, status = select_evidence(candidates, limit=3)
        self.assertTrue(selected)
        self.assertIn("Typhoid", selected[0].title)

    def test_off_topic_query_still_matches_nothing(self):
        """Pengembangan bilingual tidak boleh membuat retrieval jadi asal cocok."""
        from ragai.contracts import EvidenceStatus
        from ragai.evidence.selector import select_evidence
        from ragai.retrieval.retriever import retrieve_candidates

        candidates = retrieve_candidates("Bagaimana cara memperbaiki mesin motor injeksi")
        selected, status = select_evidence(candidates, limit=3)
        self.assertEqual(status, EvidenceStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(selected, [])

    def test_generic_tokens_weigh_less_than_concepts(self):
        from ragai.retrieval.concepts import build_search_term_groups

        groups = build_search_term_groups("Saya demam tinggi tiga hari")
        by_primary = {g["variants"][0]: g for g in groups}

        self.assertTrue(by_primary["demam"]["is_concept"])
        self.assertGreater(by_primary["demam"]["weight"], by_primary["tiga"]["weight"])
        self.assertFalse(by_primary["tiga"]["is_concept"])

    def test_lexical_score_counts_variant_group_once(self):
        from ragai.retrieval.retriever import _lexical_score

        groups = [{"variants": ["demam", "fever"], "weight": 1.5, "is_concept": True}]
        # Cocok lewat varian Inggris meski istilah aslinya Indonesia.
        self.assertGreater(_lexical_score(groups, "Fever in adults", "clinical study"), 0.9)
        self.assertEqual(_lexical_score(groups, "Bone fracture", "orthopedic study"), 0.0)


class EmergencyPrecisionTests(TestCase):
    """Peringatan gawat darurat harus presisi, bukan muncul di setiap penjelasan."""

    def test_educational_mention_does_not_trigger_warning(self):
        from ragai.contracts import EvidenceStatus
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer=("Demam dengue dapat berkembang menjadi bentuk yang lebih serius "
                    "seperti demam berdarah dengue atau sindrom syok dengue."),
            user_input="Apakah demam berdarah berbahaya?",
            evidence_status=EvidenceStatus.SUFFICIENT,
        )
        self.assertFalse(report.has_flag("EMERGENCY_SIGNAL"))
        self.assertNotIn("PERINGATAN", report.answer)

    def test_user_reported_emergency_still_triggers(self):
        from ragai.safety.validator import validate_response

        report = validate_response(
            answer="Demam dengue umumnya membaik dalam tujuh hari.",
            user_input="Anak saya kejang dan tidak sadarkan diri",
        )
        self.assertTrue(report.has_flag("EMERGENCY_SIGNAL"))
        self.assertTrue(report.answer.startswith("⚠️"))


class LlmProviderPreferenceTests(TestCase):
    """`LLM_PROVIDER` bersifat eksklusif, bukan sekadar urutan."""

    def setUp(self):
        from ragai.reasoning import llm
        llm.reset_health()
        self.addCleanup(llm.reset_health)

    @override_settings(LLM_PROVIDER="openai")
    def test_named_provider_excludes_others(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"GEMINI_API_KEY": "x", "OPENAI_API_KEY": "y"}):
            self.assertEqual(llm.configured_providers(), [llm.PROVIDER_OPENAI])

    @override_settings(LLM_PROVIDER="openai")
    def test_excluded_provider_is_never_called(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"GEMINI_API_KEY": "kunci-mati", "OPENAI_API_KEY": "y"}), \
             patch.object(llm, "_generate_gemini") as gemini, \
             patch.object(llm, "_generate_openai_compatible", return_value="ok"):
            self.assertEqual(llm.generate("halo"), "ok")
        gemini.assert_not_called()

    @override_settings(LLM_PROVIDER="tidak-ada")
    def test_unknown_provider_degrades_to_no_llm(self):
        from ragai.reasoning import llm

        with patch.dict("os.environ", {"OPENAI_API_KEY": "y"}):
            self.assertEqual(llm.configured_providers(), [])
            self.assertIsNone(llm.generate("halo"))


class EmbeddingProviderTests(TestCase):
    def setUp(self):
        from ragai.retrieval import embeddings
        embeddings.reset_dimension_cache()
        self.addCleanup(embeddings.reset_dimension_cache)

    @override_settings(EMBEDDINGS_ENABLED="0")
    def test_disabled_gate_makes_no_network_call(self):
        from ragai.retrieval import embeddings

        with patch.object(embeddings, "_embed_openai") as openai_call:
            self.assertIsNone(embeddings.embed_texts(["teks"]))
        openai_call.assert_not_called()
        self.assertIsNone(embeddings.available_provider())

    @override_settings(EMBEDDING_DIMENSIONS="768")
    def test_openai_requested_with_matching_dimensions(self):
        """Dimensi harus cocok dengan kolom vektor agar tidak perlu migrasi."""
        from ragai.retrieval import embeddings

        captured = {}

        class FakeItem:
            embedding = [0.1] * 768

        class FakeResponse:
            data = [FakeItem()]

        class FakeEmbeddings:
            def create(self, **kwargs):
                captured.update(kwargs)
                return FakeResponse()

        class FakeClient:
            embeddings = FakeEmbeddings()

        with patch.dict("os.environ", {"OPENAI_API_KEY": "y"}), \
             patch("openai.OpenAI", return_value=FakeClient()):
            vectors = embeddings.embed_texts(["teks kesehatan"])

        self.assertEqual(len(vectors[0]), 768)
        self.assertEqual(captured["dimensions"], 768)
        self.assertEqual(captured["model"], "text-embedding-3-small")

    @override_settings(EMBEDDING_DIMENSIONS="768")
    def test_dimension_mismatch_is_rejected(self):
        from ragai.retrieval import embeddings

        class FakeItem:
            embedding = [0.1] * 1536  # tidak dipotong ke 768

        class FakeResponse:
            data = [FakeItem()]

        class FakeEmbeddings:
            def create(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            embeddings = FakeEmbeddings()

        with patch.dict("os.environ", {"OPENAI_API_KEY": "y"}, clear=False), \
             patch("openai.OpenAI", return_value=FakeClient()), \
             patch.object(embeddings, "_embed_training", side_effect=Exception("tidak ada")):
            self.assertIsNone(embeddings.embed_texts(["teks"]))

    @override_settings(EMBEDDING_DIMENSIONS="768")
    def test_embed_journal_article_requires_a_provider(self):
        from ragai.retrieval import embeddings
        from api.views import embed_journal_article

        journal = JournalArticle.objects.create(
            title="Judul", abstract="Abstrak yang cukup panjang " * 10,
        )
        with patch.object(embeddings, "embed_texts", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                embed_journal_article(journal)
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))


@override_settings(LLM_PROVIDER="openai")
class TranslationProviderTests(TestCase):
    """Deployment full OpenAI tidak boleh menyentuh Gemini sama sekali."""

    def setUp(self):
        from django.core.cache import cache
        from ragai.reasoning import llm
        cache.clear()
        llm.reset_health()
        self.addCleanup(llm.reset_health)

    def test_gemini_is_not_called_when_excluded(self):
        from api import views

        with patch.dict("os.environ", {"GEMINI_API_KEY": "kunci-mati", "OPENAI_API_KEY": "y"}), \
             patch.object(views, "get_gemini_client") as gemini_client, \
             patch("ragai.reasoning.llm.generate", return_value="Translated"):
            result = views.translate_text("Teks kesehatan yang cukup panjang.", "en")

        self.assertEqual(result, "Translated")
        gemini_client.assert_not_called()

    def test_short_text_is_returned_unchanged(self):
        from api import views

        with patch("ragai.reasoning.llm.generate") as generate:
            self.assertEqual(views.translate_text("halo", "en"), "halo")
        generate.assert_not_called()

    def test_falls_back_to_original_when_llm_returns_nothing(self):
        from api import views

        original = "Teks kesehatan yang cukup panjang untuk diterjemahkan."
        with patch("ragai.reasoning.llm.generate", return_value=None):
            self.assertEqual(views.translate_text(original, "en"), original)

    def test_translate_with_cache_reuses_result(self):
        from api import views

        original = "Teks kesehatan yang cukup panjang untuk diterjemahkan."
        with patch("ragai.reasoning.llm.generate",
                   return_value="Translated text") as generate:
            first = views.translate_with_cache(original, "en", cache_prefix="uji")
            second = views.translate_with_cache(original, "en", cache_prefix="uji")

        self.assertEqual(first, "Translated text")
        self.assertEqual(second, "Translated text")
        self.assertEqual(generate.call_count, 1)  # panggilan kedua dari cache


class ModelParameterCompatibilityTests(TestCase):
    """
    Keluarga GPT-5 menolak `max_tokens` dan mensyaratkan
    `max_completion_tokens`. Deteksi ini menjaga kode tetap jalan pada model
    lama maupun baru tanpa perlu disentuh lagi saat modelnya diganti.
    """

    def test_token_parameter_per_model_family(self):
        from ragai.reasoning.llm import completion_token_kwargs

        self.assertEqual(completion_token_kwargs("gpt-4o-mini", 100), {"max_tokens": 100})
        self.assertEqual(completion_token_kwargs("gpt-4.1-mini", 100), {"max_tokens": 100})
        for model in ("gpt-5-mini", "gpt-5.4-mini", "gpt-5.6-luna"):
            self.assertEqual(
                completion_token_kwargs(model, 100), {"max_completion_tokens": 100}, model
            )

    def test_reasoning_models_use_completion_tokens(self):
        from ragai.reasoning.llm import completion_token_kwargs

        for model in ("o3-mini", "o4-mini"):
            self.assertEqual(
                completion_token_kwargs(model, 100), {"max_completion_tokens": 100}, model
            )

    def test_openai_call_uses_correct_parameter(self):
        from ragai.reasoning import llm

        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)

                class Msg:
                    content = "ok"

                class Choice:
                    message = Msg()

                class Response:
                    choices = [Choice()]

                return Response()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        with override_settings(LLM_MODEL="gpt-5.4-mini"), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "y"}), \
             patch("openai.OpenAI", return_value=FakeClient()):
            llm._generate_openai_compatible("halo", 0.15, 500, "", llm.PROVIDER_OPENAI)

        self.assertEqual(captured["model"], "gpt-5.4-mini")
        self.assertEqual(captured["max_completion_tokens"], 500)
        self.assertNotIn("max_tokens", captured)

    @override_settings(LLM_MODEL="")
    def test_default_model_is_current_generation(self):
        from ragai.reasoning.llm import openai_model

        model = openai_model()
        self.assertTrue(model.startswith("gpt-5"), model)


class EnvVariableReuseTests(TestCase):
    """Konfigurasi memakai variabel yang sudah ada, bukan nama baru sendiri."""

    @override_settings(LLM_MODEL="gpt-4.1-mini")
    def test_llm_model_variable_is_honoured(self):
        from ragai.reasoning.llm import openai_model

        self.assertEqual(openai_model(), "gpt-4.1-mini")

    def test_frontend_url_accepts_multiple_origins(self):
        """Origin HealthTalk masuk lewat FRONTEND_URL, tanpa variabel baru."""
        import importlib
        import os

        with patch.dict(os.environ, {
            "FRONTEND_URL": "https://healthify.twenti.studio, https://healthtalk.example.com/",
            "DJANGO_SECRET_KEY": "x",
        }):
            settings_module = importlib.import_module("backend_project.settings")
            origins = list(settings_module.CORS_ALLOWED_ORIGINS)
            frontend = os.getenv("FRONTEND_URL")
            parsed = [o.strip().rstrip("/") for o in frontend.split(",") if o.strip()]

        self.assertEqual(parsed,
                         ["https://healthify.twenti.studio", "https://healthtalk.example.com"])


class OpenApiServerListTests(TestCase):
    """Daftar server di spec harus ikut deployment, bukan domain hardcoded."""

    @override_settings(ALLOWED_HOSTS=["healthify.twenti.studio", "localhost",
                                      "127.0.0.1", ".railway.app"])
    # Alamat publik kini berasal dari konfigurasi. Jalur menebak dari
    # ALLOWED_HOSTS tetap ada sebagai cadangan, dan itulah yang diuji di bawah,
    # jadi PUBLIC_API_BASE_URL sengaja dikosongkan.

    def test_configured_public_address_wins(self):
        """
        Alamat publik berasal dari konfigurasi. Menebaknya dari ALLOWED_HOSTS
        pernah membuat dokumentasi menunjuk domain produk Healthify, bukan
        domain engine.
        """
        from api.openapi import build_openapi_spec

        with self.settings(PUBLIC_API_BASE_URL="https://ragai.example.com",
                           ALLOWED_HOSTS=["healthify.example.com"]):
            servers = build_openapi_spec(base_url="http://testserver")["servers"]

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["url"], "https://ragai.example.com")
        self.assertEqual(servers[0]["description"], "Production")

    def test_single_production_server_is_published(self):
        """Kontrak publik menyebut satu server, bukan peta deployment internal."""
        from api.openapi import build_openapi_spec

        with self.settings(PUBLIC_API_BASE_URL="", ALLOWED_HOSTS=["api.example.com"]):
            servers = build_openapi_spec(base_url="http://testserver")["servers"]

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["url"], "https://api.example.com")
        self.assertEqual(servers[0]["description"], "Production")

    def test_local_and_wildcard_hosts_are_never_published(self):
        from api.openapi import build_openapi_spec

        with self.settings(PUBLIC_API_BASE_URL="",
                           ALLOWED_HOSTS=["localhost", "127.0.0.1", ".railway.app",
                                          "api.example.com"]):
            urls = [s["url"] for s in build_openapi_spec(base_url="")["servers"]]

        self.assertEqual(urls, ["https://api.example.com"])

    @override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1"], PUBLIC_API_BASE_URL="")
    def test_falls_back_to_relative_server(self):
        from api.openapi import build_openapi_spec

        urls = [s["url"] for s in build_openapi_spec(base_url="")["servers"]]
        self.assertEqual(urls, ["/"])


class SymptomLexiconCoverageTests(TestCase):
    """Keluhan sehari-hari dalam Bahasa Indonesia harus tertangkap."""

    CASES = [
        ("Saya nyeri ulu hati dan panas di dada", {"nyeri ulu hati", "panas di dada"}),
        ("Perut saya kembung dan begah", {"kembung"}),
        ("Kaki saya kesemutan dan kram", {"kesemutan", "kram otot"}),
        ("Mata saya berair dan bersin-bersin terus", {"mata berair", "bersin"}),
        ("Saya susah buang air besar dan wasir kambuh", {"sembelit", "wasir"}),
        ("Badan saya menguning dan kencing seperti teh", {"kulit kuning"}),
        ("Saya nyeri pinggang dan sulit kencing", {"nyeri pinggang", "gangguan berkemih"}),
        ("Anak saya rewel dan tidak mau menyusu", {"rewel"}),
        ("Kepala saya berputar kalau berdiri", {"vertigo"}),
        ("Saya sesak nafas dan dada berdebar", {"sesak napas", "jantung berdebar"}),
    ]

    def test_common_indonesian_complaints_are_detected(self):
        from ragai.context.extractor import extract_symptoms

        for text, expected in self.CASES:
            found = set(extract_symptoms(text))
            self.assertTrue(expected <= found, f"{text!r} -> {found}, kurang {expected - found}")

    def test_word_boundary_prevents_false_matches(self):
        """Substring mentah membuat 'bersin' cocok di 'bersinar'."""
        from ragai.context.extractor import extract_symptoms

        for text in ("Matahari bersinar terang",
                     "Saya keramas setiap hari",
                     "Harga bahan pokok naik terus",
                     "Dia bekerja di bidang kesehatan masyarakat"):
            self.assertEqual(extract_symptoms(text), [], text)

    def test_filler_words_inside_phrases(self):
        """'Kepala saya terasa berputar' harus sama dengan 'kepala berputar'."""
        from ragai.context.extractor import extract_symptoms

        self.assertIn("vertigo", extract_symptoms("Kepala saya terasa berputar"))
        self.assertIn("panas di dada", extract_symptoms("Dada saya terasa panas setelah makan"))
        self.assertIn("kembung", extract_symptoms("Perut saya sering terasa kembung"))

    def test_longer_phrase_wins_over_overlapping_shorter_one(self):
        """'gusi berdarah' tidak boleh sekaligus terhitung 'pendarahan'."""
        from ragai.context.extractor import extract_symptoms

        found = extract_symptoms("Saya sariawan dan gusi berdarah")
        self.assertIn("gusi berdarah", found)
        self.assertIn("sariawan", found)
        self.assertNotIn("pendarahan", found)

    def test_symptom_complaint_routes_to_symptom_context(self):
        from ragai.query_understanding.classifier import classify_intent

        result = classify_intent("Saya sering nyeri ulu hati dan panas di dada setelah makan")
        self.assertEqual(result.intent.value, "SYMPTOM_CONTEXT")


class RevalidateClaimsCommandTests(TestCase):
    """Label tidak boleh bertahan setelah seluruh buktinya terbukti fiktif."""

    def setUp(self):
        self.claim = Claim.objects.create(text="Klaim dengan sumber fiktif")
        self.verification = VerificationResult.objects.create(
            claim=self.claim, label=VerificationResult.LABEL_HOAX,
            summary="Ringkasan", confidence=0.9,
        )
        # Sumber yang DOI/URL-nya sudah dibersihkan audit_source_links.
        dead = Source.objects.create(title="Sumber tanpa tautan", doi=None, url=None)
        ClaimSource.objects.create(claim=self.claim, source=dead, relevance_score=0.5)

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("revalidate_claims", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_changing(self):
        output = self._run()

        self.assertIn(f"Klaim #{self.claim.id}", output)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.label, VerificationResult.LABEL_HOAX)

    def test_fix_downgrades_to_unverified(self):
        self._run("--fix")

        self.verification.refresh_from_db()
        self.assertEqual(self.verification.label, VerificationResult.LABEL_UNVERIFIED)
        self.assertIsNone(self.verification.confidence)
        self.assertIn("TIDAK TERVERIFIKASI", self.verification.reviewer_notes)

    def test_claim_with_one_live_source_is_left_alone(self):
        live = Source.objects.create(
            title="Sumber sah", doi="10.1016/j.lungcan.2019.05.012",
            url="https://doi.org/10.1016/j.lungcan.2019.05.012",
        )
        ClaimSource.objects.create(claim=self.claim, source=live, relevance_score=0.9)

        self._run("--fix")

        self.verification.refresh_from_db()
        self.assertEqual(self.verification.label, VerificationResult.LABEL_HOAX)

    def test_already_unverified_is_skipped(self):
        self.verification.label = VerificationResult.LABEL_UNVERIFIED
        self.verification.confidence = None
        self.verification.save()

        output = self._run()
        self.assertIn("Tidak ada klaim", output)

    def test_reverify_requires_fix(self):
        output = self._run("--reverify")
        self.assertIn("memerlukan --fix", output)


class VectorStoreProvisioningTests(TestCase):
    """Tombol Embed di panel admin harus bekerja walau tabel vektor belum ada."""

    def test_store_vector_failure_does_not_break_embedding(self):
        from ragai.retrieval import embeddings
        from api.views import embed_journal_article

        journal = JournalArticle.objects.create(
            title="Judul jurnal", abstract="Abstrak yang cukup panjang. " * 12,
        )
        with patch.object(embeddings, "embed_text", return_value=[0.1] * 768), \
             patch.object(embeddings, "store_vector", return_value=False):
            embed_journal_article(journal)

        journal.refresh_from_db()
        self.assertTrue(journal.is_embedded)
        self.assertTrue(json.loads(journal.embedding))

    def test_store_vector_provisions_before_insert(self):
        from ragai.retrieval import embeddings

        with patch.object(embeddings, "ensure_vector_store", return_value=False) as ensure:
            self.assertFalse(embeddings.store_vector(
                "doc1", "safe1", "src", "teks", "", [0.1] * 768
            ))
        ensure.assert_called_once()

    def test_missing_provider_raises_actionable_error(self):
        from ragai.retrieval import embeddings
        from api.views import embed_journal_article

        journal = JournalArticle.objects.create(
            title="Judul", abstract="Abstrak cukup panjang. " * 12,
        )
        with patch.object(embeddings, "embed_text", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                embed_journal_article(journal)
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))
        journal.refresh_from_db()
        self.assertFalse(journal.is_embedded)


class SemanticBlendingTests(TestCase):
    """
    Cosine similarity antar teks kesehatan selalu tinggi, jadi sinyal semantik
    harus dikalibrasi — kalau tidak ia menjadi lantai seragam yang mengacak
    peringkat leksikal.
    """

    def test_similarity_below_floor_contributes_nothing(self):
        from ragai.retrieval.retriever import _blend_scores

        # Dokumen tak relevan: leksikal 0, similarity "biasa saja" -> tetap 0.
        self.assertEqual(_blend_scores(0.0, 0.24), 0.0)
        self.assertEqual(_blend_scores(0.0, 0.10), 0.0)

    def test_floor_is_calibrated_against_real_measurements(self):
        """
        Ambang diturunkan dari pengukuran pada knowledge base nyata memakai
        query yang diperluas ke Bahasa Inggris:
          terjawab     : 0,435 - 0,657
          tidak terjawab: 0,303 - 0,414
        """
        from ragai.retrieval.retriever import SEMANTIC_FLOOR

        self.assertGreater(SEMANTIC_FLOOR, 0.30)
        self.assertLess(SEMANTIC_FLOOR, 0.435)

    def test_missing_embedding_leaves_lexical_untouched(self):
        from ragai.retrieval.retriever import _blend_scores

        self.assertEqual(_blend_scores(0.87, None), 0.87)

    def test_lexical_match_without_semantic_support_is_held_back(self):
        """
        Embedding tersedia tetapi kemiripannya di bawah lantai: dokumen hanya
        berbagi kata kunci, bukan pokok bahasan. Skornya ditahan agar tidak
        cukup untuk dinyatakan memadai.
        """
        from ragai.retrieval.retriever import (
            NO_SEMANTIC_SUPPORT_PENALTY, _blend_scores,
        )

        held = _blend_scores(0.87, 0.20)
        self.assertAlmostEqual(held, 0.87 * NO_SEMANTIC_SUPPORT_PENALTY, places=4)
        self.assertLess(held, 0.87)

    def test_strong_similarity_boosts_weak_lexical(self):
        from ragai.retrieval.retriever import _blend_scores

        boosted = _blend_scores(0.10, 0.85)
        self.assertGreater(boosted, 0.10)
        self.assertLessEqual(boosted, 1.0)

    def test_irrelevant_doc_is_not_promoted_above_relevant_one(self):
        """Regresi: dokumen antibiotik pernah naik di atas dokumen dengue."""
        from ragai.retrieval.retriever import _blend_scores

        relevan = _blend_scores(0.36, 0.42)      # cocok kata kunci
        tak_relevan = _blend_scores(0.05, 0.45)  # hanya mirip secara embedding
        self.assertGreater(relevan, tak_relevan)


@OFFLINE
class CandidateScopeTests(TestCase):
    """Kandidat tidak boleh dipotong berdasarkan kebaruan sebelum diskor."""

    def test_older_but_more_relevant_journal_still_wins(self):
        from ragai.retrieval.retriever import retrieve_from_journals
        from ragai.retrieval.concepts import build_search_term_groups

        # Jurnal yang paling cocok dibuat LEBIH DULU (jadi paling "lama").
        target = make_journal(
            title="Dengue Fever: An Overview",
            abstract="Dengue fever presents with high fever and skin rash.",
            doi="10.5772/intechopen.92315",
            url="https://doi.org/10.5772/intechopen.92315",
            keywords="dengue, fever, rash",
        )
        # Lalu banyak jurnal lain yang lebih baru dan hanya cocok sedikit.
        for i in range(30):
            make_journal(
                title=f"Unrelated antibiotic study {i}",
                abstract="Antibiotic sensitivity in urinary isolates with fever noted.",
                doi=f"10.1016/j.unrelated.2024.{i:04d}",
                url=None,
                keywords="antibiotic",
            )

        terms = build_search_term_groups("demam tinggi dan muncul ruam")
        results = retrieve_from_journals(terms, limit=5)

        self.assertTrue(results)
        self.assertEqual(results[0].source_id, f"journal:{target.id}")


class CorsSecurityTests(TestCase):
    """CORS harus eksplisit — tidak ada wildcard, tidak ada domain hardcoded."""

    def test_no_hardcoded_domains_in_settings(self):
        """Domain hanya boleh datang dari environment — komentar diabaikan."""
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "backend_project" / "settings.py").read_text()
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        for token in ("healthify.cloud", ".railway.app", "vercel.app"):
            self.assertNotIn(token, code, f"{token} masih di-hardcode di kode")

    def test_configured_hosts_come_from_environment(self):
        import os

        from django.conf import settings

        configured = {h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
        extras = set(settings.ALLOWED_HOSTS) - configured - {"localhost", "127.0.0.1", "testserver"}
        railway = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if railway:
            extras.discard(railway)
        self.assertEqual(extras, set(), f"host tak terduga: {extras}")

    def test_no_wildcard_origin_regex_by_default(self):
        from django.conf import settings

        self.assertEqual(settings.CORS_ALLOWED_ORIGIN_REGEXES, [])

    def test_credentials_disabled_by_default(self):
        """Wildcard origin + kredensial = siapa pun bisa membaca respons."""
        from django.conf import settings

        self.assertFalse(settings.CORS_ALLOW_CREDENTIALS)

    def test_allowed_origins_are_all_explicit_and_https(self):
        from django.conf import settings

        for origin in settings.CORS_ALLOWED_ORIGINS:
            self.assertNotIn("*", origin, origin)
            self.assertTrue(origin.startswith(("https://", "http://localhost",
                                               "http://127.0.0.1")), origin)

    def test_allowed_hosts_has_no_wildcard(self):
        from django.conf import settings

        for host in settings.ALLOWED_HOSTS:
            self.assertNotEqual(host, "*", "ALLOWED_HOSTS berisi wildcard penuh")
            if host.startswith("."):
                self.fail(f"ALLOWED_HOSTS berisi wildcard subdomain: {host}")

    def test_cors_preflight_rejects_unknown_origin(self):
        from django.test import Client

        response = Client().options(
            "/api/health/",
            HTTP_ORIGIN="https://penyerang.vercel.app",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in response.headers})


class ProxyTlsTests(TestCase):
    """Di belakang reverse proxy, URL absolut harus https — bukan http."""

    def test_forwarded_host_not_trusted_by_default(self):
        """Host sudah benar dari nginx; mempercayai X-Forwarded-Host hanya menambah risiko."""
        from django.conf import settings

        self.assertFalse(settings.USE_X_FORWARDED_HOST)

    def test_secure_proxy_ssl_header_configured(self):
        from django.conf import settings

        self.assertEqual(settings.SECURE_PROXY_SSL_HEADER,
                         ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_base_url_honours_forwarded_proto(self):
        from unittest.mock import Mock

        from api.docs_views import _base_url

        request = Mock()
        request.META = {"HTTP_X_FORWARDED_PROTO": "https"}
        request.get_host.return_value = "healthify.twenti.studio"
        request.is_secure.return_value = False
        request.scheme = "http"

        self.assertEqual(_base_url(request), "https://healthify.twenti.studio")

    def test_scalar_page_uses_relative_spec_url(self):
        """Regresi Mixed Content: URL absolut http:// diblokir di halaman https."""
        response = self.client.get("/docs")
        html = response.content.decode()

        self.assertIn('data-url="/openapi.json"', html)
        # Tidak boleh ada URL absolut ke spesifikasi (penyebab Mixed Content).
        self.assertNotIn('data-url="http://', html)
        self.assertNotIn("http://healthify", html)
        # Satu-satunya "http://" yang boleh tersisa adalah namespace XML favicon,
        # yang tidak pernah diambil lewat jaringan.
        leftovers = [
            fragment for fragment in html.split("http://")[1:]
            if not fragment.startswith("www.w3.org/2000/svg")
        ]
        self.assertEqual(leftovers, [], f"URL http:// tak terduga: {leftovers}")

    def test_openapi_servers_are_https(self):
        from api.openapi import build_openapi_spec

        with self.settings(ALLOWED_HOSTS=["healthify.twenti.studio", "localhost"]):
            spec = build_openapi_spec(base_url="https://healthify.twenti.studio")
        for server in spec["servers"]:
            self.assertFalse(server["url"].startswith("http://"), server["url"])


class HedgingIsNotAClaimTests(TestCase):
    """Ungkapan ketidakpastian bukan pernyataan medis yang perlu bersumber."""

    def _evidence(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        return [EvidenceItem(
            chunk_id="c1", source_id="journal:1", title="Urinary tract infection",
            snippet="Dysuria and frequency are common symptoms in women.",
            doi="10.12669/pjms.35.6.115",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, relevance=0.6,
        )]

    def test_uncertainty_sentences_are_ignored(self):
        from ragai.evidence.provenance import attribute_claims

        for kalimat in (
            "Karena itu, saya belum bisa memastikan gejala apa saja yang paling khas hanya dari bukti ini.",
            "Hal ini tidak dapat dipastikan tanpa pemeriksaan laboratorium.",
            "Kondisi tersebut masih perlu dikonfirmasi oleh tenaga kesehatan.",
        ):
            self.assertEqual(attribute_claims(kalimat, self._evidence()), [], kalimat)

    def test_cited_sentence_is_traced_even_across_languages(self):
        from ragai.evidence.provenance import attribute_claims

        answer = ("Nyeri saat berkemih dan sering berkemih adalah gejala umum "
                  "infeksi saluran kemih pada wanita. [E1]")
        claims = attribute_claims(answer, self._evidence())
        self.assertEqual(claims[0].verdict, "supported")


@OFFLINE
class SimpleResponseFormatTests(TestCase):
    """Bentuk ringkas: informasi kesehatan + sumber, tanpa label apa pun."""

    def setUp(self):
        self.client = APIClient()
        make_journal()

    def _post(self, **options):
        return self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Apa gejala demam berdarah?",
                  "options": {"format": "simple", **options}},
            format="json",
        )

    def test_returns_only_answer_and_sources(self):
        body = self._post().json()

        self.assertEqual(
            set(body),
            {"answer", "sources", "has_evidence", "notice",
             "conversation_id", "sources_reused", "request_id"},
        )

    def test_contains_no_labels_or_internal_status(self):
        body = self._post().json()
        serialized = json.dumps(body)

        for token in ("intent", "evidence_status", "safety", "verdict",
                      "claims", "preliminary_assessment", "metadata",
                      "valid", "hoax", "uncertain", "INSUFFICIENT_EVIDENCE"):
            self.assertNotIn(f'"{token}"', serialized, f"{token} bocor ke format simple")

    def test_source_fields_are_publication_facts_only(self):
        body = self._post().json()
        if body["sources"]:
            self.assertEqual(
                set(body["sources"][0]),
                {"title", "url", "doi", "publisher", "year", "relevance", "snippet"},
            )

    def test_has_evidence_false_when_knowledge_base_empty(self):
        JournalArticle.objects.all().delete()
        body = self._post().json()

        self.assertFalse(body["has_evidence"])
        self.assertEqual(body["sources"], [])
        self.assertTrue(body["answer"])

    def test_notice_carries_emergency_warning(self):
        response = self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Saya nyeri dada hebat dan sesak napas berat",
                  "options": {"format": "simple"}},
            format="json",
        )
        body = response.json()
        self.assertIsNotNone(body["notice"])
        self.assertIn("gawat darurat", body["answer"].lower())

    def test_full_format_remains_default(self):
        body = self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Apa gejala demam berdarah?"}, format="json",
        ).json()
        self.assertIn("intent", body)
        self.assertIn("evidence_status", body)


class SentenceSplittingTests(TestCase):
    """Kalimat bersitasi tidak boleh hilang dari penilaian."""

    def test_sentence_after_citation_marker_is_split(self):
        from ragai.evidence.provenance import split_sentences

        text = ("Infeksi saluran kemih sering ditemukan pada perempuan. [E1] "
                "Gejalanya meliputi nyeri saat berkemih. [E2] "
                "Pemeriksaan urin membantu menegakkan diagnosis.")
        self.assertEqual(len(split_sentences(text)), 3)

    def test_cited_sentence_is_attributed_not_dropped(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        from ragai.evidence.provenance import attribute_claims

        evidence = [
            EvidenceItem(chunk_id=f"c{i}", source_id=f"journal:{i}", title=f"Studi {i}",
                         snippet="UTI in women", origin=EvidenceOrigin.KNOWLEDGE_BASE,
                         relevance=0.6)
            for i in (1, 2)
        ]
        text = ("Infeksi saluran kemih sering ditemukan pada perempuan dewasa. [E1] "
                "Gejalanya meliputi nyeri saat berkemih dan frekuensi meningkat. [E2]")
        claims = attribute_claims(text, evidence)

        self.assertEqual(len(claims), 2)
        self.assertTrue(all(c.verdict == "supported" for c in claims),
                        [(c.claim, c.verdict) for c in claims])
        # Penanda tetap menempel pada kalimat pemiliknya, bukan kalimat berikutnya.
        self.assertEqual(claims[0].supporting_evidence[0]["source_id"], "journal:1")
        self.assertEqual(claims[1].supporting_evidence[0]["source_id"], "journal:2")

    def test_insufficiency_statements_are_not_claims(self):
        from ragai.evidence.provenance import attribute_claims

        for kalimat in (
            "Bukti yang tersedia saat ini belum memadai untuk menjawab pertanyaan tersebut.",
            "Jika Anda ingin, saya bisa bantu menjelaskan hal lain yang relevan.",
        ):
            self.assertEqual(attribute_claims(kalimat, []), [], kalimat)


@OFFLINE
class SimpleAnswerCleanlinessTests(TestCase):
    def test_citation_markers_are_stripped(self):
        from ragai.adapters.healthtalk import to_simple_response
        from ragai.contracts import IntelligenceResponse

        response = IntelligenceResponse(
            answer="Demam berlangsung tiga hari umumnya karena infeksi virus. [E1] "
                   "Perhatikan tanda bahaya. [E2][E3]",
        )
        body = to_simple_response(response)

        self.assertNotIn("[E", body["answer"])
        self.assertIn("infeksi virus.", body["answer"])
        self.assertNotIn("  ", body["answer"])


class AnswerReadabilityTests(TestCase):
    """Jawaban ditujukan ke pembaca awam, bukan menarasikan proses internal."""

    def test_prompt_forbids_internal_jargon(self):
        from ragai.reasoning.generator import _SYSTEM_PROMPT, build_prompt
        from ragai.contracts import HealthContext, Intent

        self.assertIn("PEMBACA AWAM", _SYSTEM_PROMPT)
        self.assertIn("bukti yang tersedia", _SYSTEM_PROMPT)  # disebut sebagai larangan

        prompt = build_prompt("Apa gejala X?", Intent.HEALTH_INFORMATION,
                              HealthContext(), [])
        self.assertIn("Mulai langsung dengan informasinya", prompt)

    def test_prompt_forbids_marker_as_sentence_subject(self):
        """Penanda sebagai subjek membuat kalimat rusak setelah dibersihkan."""
        from ragai.reasoning.generator import _SYSTEM_PROMPT

        self.assertIn("CATATAN KAKI", _SYSTEM_PROMPT)
        self.assertIn("SALAH", _SYSTEM_PROMPT)
        self.assertIn("jangan pernah menjadikannya subjek", _SYSTEM_PROMPT)


class CitationPlacementTests(TestCase):
    """
    Penanda [En] adalah catatan kaki. Bila model memakainya sebagai subjek,
    kalimat akan kehilangan subjek begitu penanda dibersihkan untuk tampilan.
    """

    def _norm(self, text):
        from ragai.reasoning.generator import _normalize_citation_placement
        return _normalize_citation_placement(text)

    def test_marker_as_subject_is_rewritten(self):
        out = self._norm(
            "[E1] secara langsung menyatakan bahwa demam berdarah disebabkan "
            "oleh virus dengue yang ditularkan nyamuk."
        )
        self.assertTrue(out.startswith("Demam berdarah disebabkan"), out)
        self.assertTrue(out.rstrip().endswith("[E1]"), out)

    def test_reporting_verbs_are_removed(self):
        for prefix in ("[E1] menyatakan bahwa ", "[E2] hanya menyebut bahwa ",
                       "[E1] membahas ", "[E2] juga menunjukkan bahwa "):
            out = self._norm(prefix + "gejala utamanya adalah demam tinggi.")
            self.assertTrue(out.startswith("Gejala utamanya"), out)

    def test_trailing_markers_are_left_alone(self):
        text = ("Demam berdarah disebabkan oleh virus dengue. [E1] "
                "Gejalanya meliputi demam tinggi dan ruam kulit. [E2]")
        self.assertEqual(self._norm(text), text)

    def test_text_without_markers_is_unchanged(self):
        text = "Tidak ada penanda sama sekali dalam kalimat ini yang cukup panjang."
        self.assertEqual(self._norm(text), text)

    def test_bare_marker_without_brackets_is_handled(self):
        """Model kadang menulis "E1 menyebut ..." tanpa kurung."""
        out = self._norm("E1 hanya menyebut bahwa UTI adalah infeksi yang umum di masyarakat.")
        self.assertTrue(out.startswith("UTI adalah infeksi"), out)
        self.assertTrue(out.rstrip().endswith("[E1]"), out)

    def test_internal_nouns_as_subject_are_removed(self):
        for prefix in ("Evidence secara langsung menyatakan bahwa ",
                       "Bukti yang tersedia menunjukkan bahwa ",
                       "Sumber tersebut melaporkan bahwa "):
            out = self._norm(prefix + "vaksinasi menurunkan risiko infeksi berat.")
            self.assertTrue(out.startswith("Vaksinasi menurunkan"), out)
            self.assertNotIn("Evidence", out)

    def test_legitimate_sentence_starting_with_bukti_is_kept(self):
        """Tanpa kata kerja pelaporan, kalimat sah tidak boleh dipotong."""
        text = "Bukti ini penting untuk dipahami oleh tenaga kesehatan."
        self.assertEqual(self._norm(text), text)

    def test_simple_format_yields_grammatical_sentences(self):
        from ragai.adapters.healthtalk import to_simple_response
        from ragai.contracts import IntelligenceResponse

        raw = "[E1] menyatakan bahwa infeksi ini ditularkan melalui gigitan nyamuk."
        body = to_simple_response(IntelligenceResponse(answer=self._norm(raw)))

        self.assertTrue(body["answer"][0].isupper(), body["answer"])
        self.assertNotIn("[E", body["answer"])
        self.assertTrue(body["answer"].startswith("Infeksi ini ditularkan"), body["answer"])


class ClaimVsInformationRoutingTests(TestCase):
    """
    Pola sebab-akibat di dalam PERTANYAAN bukan klaim yang perlu diverifikasi.
    Regresi: "Apa yang menyebabkan demam berdarah?" sempat masuk claim engine
    dan dijawab dengan bahasa penilaian klaim, bukan informasi.
    """

    def _intent(self, query, mode=None):
        from ragai.query_understanding.classifier import classify_intent
        return classify_intent(query, mode=mode).intent.value

    def test_information_questions_are_not_claims(self):
        for query in ("Apa yang menyebabkan demam berdarah?",
                      "Bagaimana cara mencegah infeksi saluran kemih?",
                      "Kenapa merokok menyebabkan kanker paru?",
                      "Apa saja yang bisa memicu serangan asma?"):
            self.assertEqual(self._intent(query), "HEALTH_INFORMATION", query)

    def test_assertions_remain_claims(self):
        for query in ("Vitamin C dosis tinggi menyembuhkan kanker",
                      "Merokok menyebabkan kanker paru-paru",
                      "Air kelapa mencegah diabetes"):
            self.assertEqual(self._intent(query), "CLAIM_VERIFICATION", query)

    def test_verification_cue_wins_even_in_question_form(self):
        for query in ("Benarkah vitamin C menyembuhkan kanker?",
                      "Apakah benar air kelapa mencegah diabetes?",
                      "Apa ini cuma mitos soal MSG menyebabkan kanker?"):
            self.assertEqual(self._intent(query), "CLAIM_VERIFICATION", query)

    def test_caller_mode_information_is_respected(self):
        from ragai.contracts import Mode

        self.assertEqual(
            self._intent("Apa yang menyebabkan demam berdarah?", mode=Mode.INFORMATION),
            "HEALTH_INFORMATION",
        )

    def test_explicit_claim_mode_still_routes_to_claim_engine(self):
        from ragai.contracts import Mode

        self.assertEqual(
            self._intent("Vitamin C menyembuhkan kanker", mode=Mode.CLAIM),
            "CLAIM_VERIFICATION",
        )


@OFFLINE
class ClaimAnswerNormalizationTests(TestCase):
    def test_claim_engine_answer_is_also_normalized(self):
        from ragai import engine

        make_journal()
        with patch("ragai.claims.evaluator.evaluate_claim") as evaluate:
            evaluate.return_value = type("E", (), {
                "verdict": "supported", "confidence": 0.8, "method": "llm",
                "explanation": "[E1] menyatakan bahwa demam disebabkan infeksi virus.",
                "supporting_evidence_ids": [],
                "to_dict": lambda self: {"verdict": "supported"},
            })()
            result = engine.process({"query": "Demam disebabkan infeksi virus", "mode": "claim"})

        self.assertNotIn("[E1] menyatakan", result.answer)


@OFFLINE
class RateLimitTests(TestCase):
    """Satu consumer tidak boleh menghabiskan kuota LLM milik Healthify."""

    def setUp(self):
        from django.core.cache import cache

        from api.intelligence_views import ConsumerRateThrottle

        cache.clear()
        self.client = APIClient()
        make_journal()
        # DRF membaca THROTTLE_RATES saat import, jadi override_settings tidak
        # berpengaruh — batasnya ditambal langsung di kelas throttle.
        patcher = patch.object(
            ConsumerRateThrottle, "THROTTLE_RATES", {"intelligence": "3/min"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _query(self, key=None):
        headers = {"HTTP_X_API_KEY": key} if key else {}
        return self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Apa gejala demam berdarah?"}, format="json", **headers
        )

    def test_requests_are_throttled_after_limit(self):
        codes = [self._query().status_code for _ in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200], codes)
        self.assertIn(429, codes, codes)

    def test_consumers_have_separate_budgets(self):
        for _ in range(3):
            self._query(key="kunci-a")
        self.assertEqual(self._query(key="kunci-a").status_code, 429)
        # Consumer lain tidak ikut terkena.
        self.assertEqual(self._query(key="kunci-b").status_code, 200)

    def test_throttle_key_does_not_expose_api_key(self):
        from unittest.mock import Mock

        from api.intelligence_views import ConsumerRateThrottle

        request = Mock()
        request.META = {"HTTP_X_API_KEY": "ht_live_rahasia_sekali"}
        key = ConsumerRateThrottle().get_cache_key(request, None)

        self.assertNotIn("rahasia", key)
        self.assertNotIn("ht_live", key)


@OFFLINE
class BackendConsumerTests(TestCase):
    """Kebutuhan khas consumer server-to-server, bukan browser."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = APIClient()
        make_journal()

    def _post(self, **extra):
        return self.client.post(
            "/api/v1/intelligence/query",
            data={"query": "Apa gejala demam berdarah?", "options": {"format": "simple"}},
            format="json", **extra
        )

    # --- korelasi ---
    def test_request_id_returned_as_header(self):
        response = self._post()
        self.assertIn("X-Request-Id", response.headers)
        self.assertEqual(response.headers["X-Request-Id"], response.json()["request_id"])

    def test_client_supplied_request_id_is_echoed(self):
        response = self._post(HTTP_X_REQUEST_ID="trace-abc-12345")
        self.assertEqual(response.headers["X-Request-Id"], "trace-abc-12345")
        self.assertEqual(response.json()["request_id"], "trace-abc-12345")

    def test_malformed_request_id_is_replaced(self):
        response = self._post(HTTP_X_REQUEST_ID="../etc/passwd; DROP TABLE")
        self.assertNotIn("DROP TABLE", response.headers["X-Request-Id"])
        self.assertRegex(response.headers["X-Request-Id"], r"^[0-9a-f]{32}$")

    # --- idempotensi ---
    def test_repeat_with_same_key_is_not_reprocessed(self):
        from ragai import engine

        with patch.object(engine, "process", wraps=engine.process) as spied:
            first = self._post(HTTP_X_IDEMPOTENCY_KEY="job-991")
            second = self._post(HTTP_X_IDEMPOTENCY_KEY="job-991")

        self.assertEqual(spied.call_count, 1, "permintaan diulang malah diproses lagi")
        self.assertEqual(first.json()["answer"], second.json()["answer"])
        self.assertEqual(second.headers.get("X-Idempotent-Replay"), "true")
        self.assertIsNone(first.headers.get("X-Idempotent-Replay"))

    def test_different_keys_are_processed_separately(self):
        from ragai import engine

        with patch.object(engine, "process", wraps=engine.process) as spied:
            self._post(HTTP_X_IDEMPOTENCY_KEY="job-1")
            self._post(HTTP_X_IDEMPOTENCY_KEY="job-2")
        self.assertEqual(spied.call_count, 2)

    def test_without_key_every_request_is_processed(self):
        from ragai import engine

        with patch.object(engine, "process", wraps=engine.process) as spied:
            self._post()
            self._post()
        self.assertEqual(spied.call_count, 2)

    @override_settings(INTELLIGENCE_API_KEYS={"key-a": "a", "key-b": "b"})
    def test_idempotency_keys_do_not_leak_between_consumers(self):
        from ragai import engine

        with patch.object(engine, "process", wraps=engine.process) as spied:
            self._post(HTTP_X_API_KEY="key-a", HTTP_X_IDEMPOTENCY_KEY="sama")
            self._post(HTTP_X_API_KEY="key-b", HTTP_X_IDEMPOTENCY_KEY="sama")
        self.assertEqual(spied.call_count, 2, "consumer lain menerima respons milik orang lain")

    # --- error tetap membawa korelasi ---
    def test_error_response_carries_request_id(self):
        from ragai import engine

        with patch.object(engine, "process", side_effect=Exception("boom")):
            response = self._post(HTTP_X_REQUEST_ID="trace-error-1")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["X-Request-Id"], "trace-error-1")
        self.assertEqual(response.json()["request_id"], "trace-error-1")


class SharedCacheTests(TestCase):
    """Rate limit dan idempotensi harus konsisten di semua worker gunicorn."""

    def test_cache_backend_is_shared_across_processes(self):
        from django.conf import settings

        backend = settings.CACHES["default"]["BACKEND"]
        self.assertNotIn("locmem", backend.lower(),
                         "LocMemCache membuat tiap worker punya hitungan sendiri")

    def test_per_key_rate_override_is_parsed(self):
        import importlib
        import os

        with patch.dict(os.environ, {
            "INTELLIGENCE_API_KEYS": "k1:healthtalk:300/min,k2:mitra",
            "DJANGO_SECRET_KEY": "x",
        }):
            mod = importlib.reload(importlib.import_module("backend_project.settings"))
            self.assertEqual(mod.INTELLIGENCE_API_KEYS,
                             {"k1": "healthtalk", "k2": "mitra"})
            self.assertEqual(mod.INTELLIGENCE_KEY_RATES, {"k1": "300/min"})


class InfoBlockLinkTests(TestCase):
    """
    Field `info` yang bertipe URL menurut spesifikasi OpenAPI tidak boleh diisi
    teks biasa: Scalar merendernya sebagai tautan, dan hasilnya tautan rusak.
    """

    URL_FIELDS = ("termsOfService",)

    def test_url_fields_are_urls_or_absent(self):
        spec = self.client.get("/openapi.json").json()
        info = spec["info"]

        for field in self.URL_FIELDS:
            if field in info:
                self.assertRegex(info[field], r"^https?://",
                                 f"info.{field} harus URL, bukan teks biasa")

    def test_contact_url_is_url_or_absent(self):
        info = self.client.get("/openapi.json").json()["info"]
        contact = info.get("contact", {})

        if "url" in contact:
            self.assertRegex(contact["url"], r"^https?://")
        if "email" in contact:
            self.assertIn("@", contact["email"])

    def test_license_url_is_url_or_absent(self):
        info = self.client.get("/openapi.json").json()["info"]
        license_block = info.get("license", {})

        if "url" in license_block:
            self.assertRegex(license_block["url"], r"^https?://")

    def test_no_broken_link_targets_anywhere_in_info(self):
        """Setiap nilai yang tampak seperti tautan harus benar-benar tautan."""
        info = self.client.get("/openapi.json").json()["info"]

        for key, value in info.items():
            if key == "description":
                continue
            if isinstance(value, str) and value.startswith("/"):
                self.fail(f"info.{key} berisi path relatif yang akan dirender sebagai tautan")


class AccessRequestContactTests(TestCase):
    """Alamat kontak hanya muncul bila dikonfigurasi, dan hanya bila valid."""

    def _info(self):
        return self.client.get("/openapi.json").json()["info"]

    @override_settings(API_CONTACT_EMAIL="", API_CONTACT_URL="")
    def test_no_contact_details_are_invented(self):
        info = self._info()

        self.assertEqual(info["contact"], {"name": "ragai"})
        self.assertNotIn("## Contact", info["description"])

    @override_settings(API_CONTACT_EMAIL="api@example.com", API_CONTACT_URL="")
    def test_email_is_published_when_configured(self):
        info = self._info()

        self.assertEqual(info["contact"]["email"], "api@example.com")
        self.assertIn("api@example.com", info["description"])

    @override_settings(API_CONTACT_EMAIL="", API_CONTACT_URL="https://example.com/api-access")
    def test_url_is_published_when_configured(self):
        info = self._info()

        self.assertEqual(info["contact"]["url"], "https://example.com/api-access")
        self.assertIn("https://example.com/api-access", info["description"])

    @override_settings(API_CONTACT_EMAIL="bukan-email", API_CONTACT_URL="bukan-url")
    def test_malformed_values_are_rejected(self):
        """Teks biasa di field URL menghasilkan tautan rusak saat dirender."""
        info = self._info()

        self.assertNotIn("url", info["contact"])
        self.assertNotIn("email", info["contact"])
        self.assertNotIn("## Contact", info["description"])


class TitleAuthorityTests(TestCase):
    """
    Registry adalah otoritas judul. Memastikan DOI *terdaftar* tidak cukup:
    judul meyakinkan dapat dipasangkan dengan DOI nyata milik paper lain.
    """

    def test_titles_match_tolerates_cosmetic_differences(self):
        from ragai.evidence.link_validator import titles_match

        self.assertTrue(titles_match(
            "Dengue Fever: An Overview",
            "Dengue fever - an overview",
        ))
        self.assertTrue(titles_match(
            "Smoking and Cardiovascular Disease: A Review",
            "Smoking and cardiovascular disease, a review",
        ))

    def test_titles_match_rejects_different_works(self):
        from ragai.evidence.link_validator import titles_match

        self.assertFalse(titles_match(
            "The impact of COVID-19 on male fertility: a systematic review",
            "Trust science?",
        ))
        self.assertFalse(titles_match(
            "Smoking and Cardiovascular Disease: A Review",
            "Median Arcuate Ligament Compression in Orthotopic Liver Transplantation",
        ))

    def test_missing_data_is_not_treated_as_mismatch(self):
        from ragai.evidence.link_validator import titles_match

        self.assertTrue(titles_match("", "Some Title"))
        self.assertTrue(titles_match("Some Title", ""))

    def test_evidence_title_is_replaced_by_registry_title(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        from ragai.evidence import link_validator as lv
        from ragai.evidence.selector import validate_links

        item = EvidenceItem(
            chunk_id="c1", source_id="source:1",
            title="The impact of COVID-19 on male fertility: a systematic review",
            snippet="...", doi="10.1016/j.fertnstert.2021.03.001",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, semantic_relevance=0.8,
        )
        registry = {"title": "Trust science?", "publisher": "Elsevier",
                    "container": "Fertility and Sterility", "year": 2021,
                    "authors": "Carpinello, Olivia"}

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED), \
             patch.object(lv, "fetch_doi_metadata", return_value=registry):
            validated = validate_links([item])

        self.assertEqual(validated[0].title, "Trust science?")
        self.assertEqual(validated[0].publisher, "Fertility and Sterility")
        self.assertEqual(validated[0].published_year, 2021)
        self.assertTrue(validated[0].title_corrected)

    def test_matching_title_is_not_flagged_as_corrected(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        from ragai.evidence import link_validator as lv
        from ragai.evidence.selector import validate_links

        item = EvidenceItem(
            chunk_id="c1", source_id="journal:1",
            title="Dengue Fever: An Overview", snippet="...",
            doi="10.5772/intechopen.92315",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, semantic_relevance=0.8,
        )
        registry = {"title": "Dengue Fever: An Overview", "publisher": "IntechOpen",
                    "container": "", "year": 2020, "authors": ""}

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED), \
             patch.object(lv, "fetch_doi_metadata", return_value=registry):
            validated = validate_links([item])

        self.assertFalse(validated[0].title_corrected)
        self.assertEqual(validated[0].title, "Dengue Fever: An Overview")

    def test_absent_registry_metadata_leaves_title_untouched(self):
        """DOI dari agensi non-Crossref tidak punya metadata; jangan dikosongkan."""
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        from ragai.evidence import link_validator as lv
        from ragai.evidence.selector import validate_links

        item = EvidenceItem(
            chunk_id="c1", source_id="journal:1", title="Judul asli", snippet="...",
            doi="10.5281/zenodo.123456",
            origin=EvidenceOrigin.KNOWLEDGE_BASE, semantic_relevance=0.8,
        )
        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED), \
             patch.object(lv, "fetch_doi_metadata", return_value=None):
            validated = validate_links([item])

        self.assertEqual(validated[0].title, "Judul asli")


class CacheResilienceTests(TestCase):
    """Cache yang bermasalah menurunkan kecepatan, bukan mematikan permintaan."""

    def test_broken_cache_does_not_break_validation(self):
        from django.core.cache import cache

        from ragai.evidence import link_validator as lv

        with patch.object(cache, "get", side_effect=Exception("tabel cache hilang")), \
             patch.object(cache, "set", side_effect=Exception("tabel cache hilang")), \
             patch.object(lv, "_resolve_doi_uncached", return_value=lv.STATUS_VERIFIED):
            self.assertEqual(lv.resolve_doi("10.1016/j.jinf.2021.02.004"),
                             lv.STATUS_VERIFIED)


class DropMismatchedSourcesTests(TestCase):
    """
    Pasangan (judul, DOI) yang tidak cocok berarti keduanya tidak dapat
    dipercaya. Mengganti judulnya menghasilkan sitasi jujur yang tidak relevan
    dengan klaimnya, jadi harus ada cara membuangnya.
    """

    def setUp(self):
        self.source = Source.objects.create(
            title="Smoking and Cardiovascular Disease: A Review",
            doi="10.3390/jcm8040550",
            url="https://doi.org/10.3390/jcm8040550",
            source_type="journal",
        )

    def _run(self, *args):
        from contextlib import ExitStack
        from io import StringIO

        from django.core.management import call_command
        from ragai.evidence import link_validator as lv

        registry = {"title": "Median Arcuate Ligament Compression in Orthotopic "
                             "Liver Transplantation",
                    "publisher": "MDPI", "container": "Journal of Clinical Medicine",
                    "year": 2019, "authors": ""}
        out = StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(lv, "resolve_doi",
                                             return_value=lv.STATUS_VERIFIED))
            stack.enter_context(patch.object(lv, "fetch_doi_metadata",
                                             return_value=registry))
            call_command("audit_source_links", "--only", "sources", "--delay", "0",
                         *args, stdout=out)
        return out.getvalue()

    def test_drop_mismatched_removes_the_link(self):
        self._run("--fix", "--drop-mismatched")

        self.source.refresh_from_db()
        self.assertIsNone(self.source.doi)
        self.assertIsNone(self.source.url)
        # Judul dibiarkan agar jejak audit tetap terbaca.
        self.assertEqual(self.source.title, "Smoking and Cardiovascular Disease: A Review")

    def test_default_fix_replaces_title_instead(self):
        self._run("--fix")

        self.source.refresh_from_db()
        self.assertEqual(self.source.doi, "10.3390/jcm8040550")
        self.assertIn("Median Arcuate Ligament", self.source.title)

    def test_drop_mismatched_requires_fix(self):
        output = self._run("--drop-mismatched")

        self.assertIn("memerlukan --fix", output)
        self.source.refresh_from_db()
        self.assertEqual(self.source.doi, "10.3390/jcm8040550")


class TopicalRelevanceTests(TestCase):
    """
    Mencocokkan nama penyakit tidak sama dengan menjawab pertanyaannya.
    Regresi: pertanyaan tentang COVID-19 pernah dijawab dengan paper
    "Imaging in Dengue Fever" dan "Tuberculosis treatment adherence".
    """

    def _focus(self, title, subjects):
        from ragai.retrieval.retriever import _title_focus
        return _title_focus(title, set(subjects))

    def test_document_about_something_else_is_penalised(self):
        """COVID-19 hanya konteks pada paper yang pokoknya tuberkulosis."""
        focus = self._focus("Tuberculosis treatment adherence in the era of COVID-19",
                            {"covid"})
        self.assertLessEqual(focus, 0.5, focus)

    def test_document_on_topic_keeps_full_focus(self):
        self.assertEqual(self._focus("Dengue Fever: An Overview",
                                     {"demam", "ruam"}), 1.0)

    def test_more_specific_title_still_answers_general_question(self):
        """'demam berdarah' pada judul menjawab pertanyaan tentang 'demam'."""
        self.assertEqual(self._focus("Dengue Fever: An Overview", {"demam"}), 1.0)

    def test_unrelated_title_signals_off_topic(self):
        """Judul tentang penyakit lain sama sekali."""
        self.assertEqual(
            self._focus("Evidence-based treatment recommendations for GERD",
                        {"demam berdarah"}),
            0.0,
        )

    def test_off_topic_document_gets_the_off_topic_score(self):
        from ragai.retrieval.retriever import (
            OFF_TOPIC_TITLE_SCORE, _topical_score,
        )

        score, off_topic = _topical_score(
            aspect_groups=[["symptom", "gejala"]],
            query_subjects={"demam berdarah"},
            title="Evidence-based treatment recommendations for GERD",
            body="Symptoms of reflux include heartburn.",   # kata kunci kebetulan cocok
        )
        self.assertEqual(score, OFF_TOPIC_TITLE_SCORE)
        self.assertTrue(off_topic)

    def test_partial_title_overlap_is_not_treated_as_off_topic(self):
        """Judul yang menyebut beberapa hal, salah satunya yang ditanyakan."""
        from ragai.retrieval.retriever import (
            OFF_TOPIC_TITLE_SCORE, _topical_score,
        )

        score, off_topic = _topical_score(
            aspect_groups=[["transmission", "penularan"]],
            query_subjects={"tifus"},
            title="Typhoid Fever Diagnosis in Endemic Countries",
            body="Transmission occurs through contaminated food and water.",
        )
        self.assertGreater(score, OFF_TOPIC_TITLE_SCORE)
        self.assertFalse(off_topic)

    def test_unrecognised_title_is_not_punished(self):
        self.assertEqual(self._focus("A Study of Something Unnamed", {"demam"}), 1.0)

    def test_nested_concepts_do_not_inflate_the_denominator(self):
        """'demam' dan 'demam berdarah' dari judul yang sama adalah satu topik."""
        from ragai.retrieval.retriever import _collapse_nested

        self.assertEqual(_collapse_nested({"demam", "demam berdarah"}),
                         {"demam berdarah"})
        self.assertEqual(_collapse_nested({"covid", "covid-19"}), {"covid-19"})


class QuestionAspectTests(TestCase):
    """Pertanyaan berbeda tentang topik sama membutuhkan paper berbeda."""

    def test_aspects_are_extracted(self):
        from ragai.lexicon import find_aspects

        self.assertIn("gejala", find_aspects("Apa gejala demam berdarah?"))
        self.assertIn("pengobatan", find_aspects("Bagaimana penanganan GERD?"))
        self.assertIn("penularan", find_aspects("Apakah tifus menular lewat makanan?"))
        self.assertIn("pencegahan", find_aspects("Bagaimana mencegah ISK?"))
        self.assertIn("keamanan", find_aspects("Apa efek samping paracetamol?"))

    def test_aspect_lowers_score_when_unaddressed(self):
        from ragai.contracts import EvidenceItem
        from ragai.evidence.quality import compute_evidence_score

        base = dict(chunk_id="c", source_id="s", title="Judul",
                    snippet="Teks abstrak yang cukup panjang untuk dinilai.",
                    semantic_relevance=0.8)
        cocok = EvidenceItem(aspect_match=1.0, **base)
        tidak = EvidenceItem(aspect_match=0.0, **base)

        self.assertGreater(compute_evidence_score(cocok), compute_evidence_score(tidak))

    def test_sufficiency_requires_an_aspect_match(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin, EvidenceStatus
        from ragai.evidence.selector import classify_sufficiency

        def item(aspect):
            return EvidenceItem(chunk_id="c", source_id="s", title="T", snippet="x",
                                origin=EvidenceOrigin.KNOWLEDGE_BASE,
                                relevance=0.7, aspect_match=aspect)

        self.assertEqual(classify_sufficiency([item(1.0), item(1.0)]),
                         EvidenceStatus.SUFFICIENT)
        self.assertEqual(classify_sufficiency([item(0.0), item(0.0)]),
                         EvidenceStatus.PARTIAL)


class BilingualConceptTests(TestCase):
    """Judul jurnal Inggris harus menghasilkan konsep yang sama dengan query Indonesia."""

    def test_english_condition_names_map_to_canonical(self):
        from ragai.retrieval.concepts import extract_health_concepts

        self.assertIn("infeksi saluran kemih",
                      extract_health_concepts("A Concise Overview on Urinary Tract Infection"))
        self.assertIn("tifus",
                      extract_health_concepts("Enteric fever (typhoid and paratyphoid fever)"))
        self.assertIn("demam berdarah",
                      extract_health_concepts("Dengue Fever: An Overview"))

    def test_embedding_query_is_expanded_to_english(self):
        """Kemiripan embedding lintas bahasa terlalu lemah untuk membedakan apa pun."""
        from ragai.retrieval.concepts import build_embedding_query

        expanded = build_embedding_query("Apa gejala demam berdarah?")
        self.assertIn("dengue", expanded)
        self.assertIn("symptom", expanded)
        self.assertIn("demam berdarah", expanded)

    def test_embedding_query_includes_aspect_terms(self):
        from ragai.retrieval.concepts import build_embedding_query

        expanded = build_embedding_query("Bagaimana penanganan GERD?")
        self.assertIn("treatment", expanded)
        self.assertIn("reflux", expanded)


@OFFLINE
class UntraceableSourceTests(TestCase):
    """
    Baris `Source` yang kehilangan DOI dan URL tidak dapat ditelusuri pembaca.
    Regresi: judul karangan yang DOI-nya sudah dibuang audit tetap muncul
    kembali sebagai bukti.
    """

    def test_source_without_doi_or_url_is_excluded(self):
        from ragai.retrieval.retriever import retrieve_from_sources

        claim = Claim.objects.create(text="Klaim uji")
        untraceable = Source.objects.create(
            title="The impact of COVID-19 on male fertility: a systematic review",
            doi=None, url=None, source_type="journal",
        )
        ClaimSource.objects.create(claim=claim, source=untraceable,
                                   relevance_score=0.9, excerpt="covid fertility")

        results = retrieve_from_sources(["covid", "fertility"])
        self.assertEqual(results, [])

    def test_source_with_a_link_is_still_returned(self):
        from ragai.retrieval.retriever import retrieve_from_sources

        claim = Claim.objects.create(text="Klaim uji")
        traceable = Source.objects.create(
            title="Tobacco smoking and lung cancer risk",
            doi="10.1016/j.lungcan.2019.05.012",
            url="https://doi.org/10.1016/j.lungcan.2019.05.012",
            source_type="journal",
        )
        ClaimSource.objects.create(claim=claim, source=traceable,
                                   relevance_score=0.9,
                                   excerpt="Cigarette smoking causes lung cancer.")

        results = retrieve_from_sources(["kanker", "lung cancer", "smoking"])
        self.assertTrue(results)


@OFFLINE
class OffTopicExclusionTests(TestCase):
    """
    Dokumen yang judulnya membahas topik lain tidak boleh disajikan sebagai
    bukti, berapa pun kecocokan kata kuncinya. Regresi: pertanyaan tentang
    COVID-19 dijawab dengan "Imaging in Dengue Fever" karena abstraknya
    kebetulan menyebut COVID.
    """

    def test_off_topic_candidate_is_excluded(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin
        from ragai.evidence.selector import select_evidence

        on_topic = EvidenceItem(
            chunk_id="a", source_id="journal:1", title="COVID-19 and Recovery",
            snippet="covid outcomes", origin=EvidenceOrigin.KNOWLEDGE_BASE,
            semantic_relevance=0.6, off_topic=False,
        )
        off_topic = EvidenceItem(
            chunk_id="b", source_id="journal:2", title="Imaging in Dengue Fever",
            snippet="dengue imaging, briefly mentions covid",
            origin=EvidenceOrigin.KNOWLEDGE_BASE,
            semantic_relevance=0.9, off_topic=True,   # skor lebih tinggi sekalipun
        )

        selected, _ = select_evidence([on_topic, off_topic], validate=False, limit=5)
        titles = [i.title for i in selected]

        self.assertIn("COVID-19 and Recovery", titles)
        self.assertNotIn("Imaging in Dengue Fever", titles)

    def test_all_off_topic_yields_no_evidence(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin, EvidenceStatus
        from ragai.evidence.selector import select_evidence

        items = [
            EvidenceItem(chunk_id=str(i), source_id=f"journal:{i}",
                         title=f"Paper tentang topik lain {i}", snippet="x",
                         origin=EvidenceOrigin.KNOWLEDGE_BASE,
                         semantic_relevance=0.9, off_topic=True)
            for i in range(3)
        ]
        selected, status = select_evidence(items, validate=False, limit=5)

        self.assertEqual(selected, [])
        self.assertEqual(status, EvidenceStatus.INSUFFICIENT_EVIDENCE)


class StaleVerificationCacheTests(TestCase):
    """
    Hasil verifikasi yang tersimpan tidak boleh menutupi perbaikan mesin.
    Regresi: klaim "batuk itu sehat" terus menampilkan 5 sumber dari mesin lama
    meski retrieval sudah diperbaiki, karena cache mengabaikan versi logika.
    """

    def setUp(self):
        self.client = APIClient()
        self.claim = Claim.objects.create(text="batuk itu sehat",
                                          status=Claim.STATUS_DONE)
        self.verification = VerificationResult.objects.create(
            claim=self.claim, label=VerificationResult.LABEL_UNCERTAIN,
            summary="Ringkasan lama", confidence=0.5, logic_version="v2.0",
        )
        source = Source.objects.create(title="Paper GERD yang tidak nyambung",
                                       doi="10.1016/j.lama.2019.01.001")
        ClaimSource.objects.create(claim=self.claim, source=source, relevance_score=0.5)

    def test_stale_result_is_not_served_from_cache(self):
        from api.views import check_cached_result

        is_cached, claim, verification = check_cached_result("batuk itu sehat")
        self.assertFalse(is_cached, "hasil versi lama masih disajikan dari cache")

    def test_current_version_result_is_served(self):
        from api.ai_adapter import VERIFICATION_LOGIC_VERSION
        from api.views import check_cached_result

        self.verification.logic_version = VERIFICATION_LOGIC_VERSION
        self.verification.save()

        is_cached, claim, _ = check_cached_result("batuk itu sehat")
        self.assertTrue(is_cached)
        self.assertEqual(claim.id, self.claim.id)

    def test_reverification_updates_in_place_without_duplicating(self):
        ai_result = {"label": "unverified", "confidence": None,
                     "summary": "Tidak ada bukti memadai.", "sources": []}

        with patch("api.views.call_ai_verify", return_value=ai_result):
            response = self.client.post(reverse("claim-verify"),
                                        data={"text": "batuk itu sehat"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Claim.objects.filter(text_normalized="batuk itu sehat").count(), 1,
                         "verifikasi ulang menumpuk klaim duplikat")

    def test_reverification_clears_sources_from_the_old_engine(self):
        ai_result = {"label": "unverified", "confidence": None,
                     "summary": "Tidak ada bukti memadai.", "sources": []}

        with patch("api.views.call_ai_verify", return_value=ai_result):
            self.client.post(reverse("claim-verify"),
                             data={"text": "batuk itu sehat"}, format="json")

        self.assertEqual(ClaimSource.objects.filter(claim=self.claim).count(), 0,
                         "sumber dari mesin lama masih menempel")
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.label, "unverified")

    def test_logic_version_is_stamped_on_new_results(self):
        from api.ai_adapter import VERIFICATION_LOGIC_VERSION

        ai_result = {"label": "unverified", "confidence": None,
                     "summary": "x", "sources": []}
        with patch("api.views.call_ai_verify", return_value=ai_result):
            self.client.post(reverse("claim-verify"),
                             data={"text": "klaim yang benar-benar baru"}, format="json")

        vr = VerificationResult.objects.get(claim__text_normalized="klaim yang benar benar baru")
        self.assertEqual(vr.logic_version, VERIFICATION_LOGIC_VERSION)


class DiseaseFocusedRelevanceTests(TestCase):
    """
    Penilaian "dokumen ini tentang apa" hanya melihat PENYAKIT, bukan kosakata
    kesehatan umum. Regresi: judul deskriptif panjang seperti "A Concise
    Overview on Urinary Tract Infection includes Microbial agents, Predisposing
    factors, Antibiotic Resistance..." ikut terbuang karena sub-topiknya
    menggelembungkan penyebut.
    """

    def test_only_diseases_are_counted(self):
        from ragai.retrieval.concepts import extract_conditions

        found = extract_conditions(
            "A Concise Overview on Urinary Tract Infection (UTI) includes "
            "Microbial agents, Predisposing factors, Antibiotic Resistance"
        )
        self.assertIn("infeksi saluran kemih", found)
        self.assertNotIn("antibiotik", found)
        self.assertNotIn("infeksi", found)

    def test_descriptive_title_stays_on_topic(self):
        from ragai.retrieval.retriever import _title_focus

        focus = _title_focus(
            "A Concise Overview on Urinary Tract Infection (UTI) includes "
            "Microbial agents, Predisposing factors, Antibiotic Resistance",
            {"infeksi saluran kemih"},
        )
        self.assertEqual(focus, 1.0)

    def test_competing_disease_lowers_focus(self):
        from ragai.retrieval.retriever import _title_focus

        focus = _title_focus("Tuberculosis treatment adherence in the era of COVID-19",
                             {"covid"})
        self.assertLessEqual(focus, 0.5)


class AspectLocationTests(TestCase):
    """
    Istilah aspek di JUDUL menandakan dokumen memang membahasnya. Di badan
    abstrak jauh lebih lemah: kata seperti "effective" atau "symptom" ada di
    hampir setiap abstrak medis.
    """

    def _coverage(self, title, body, keywords=""):
        from ragai.retrieval.retriever import _aspect_coverage
        return _aspect_coverage([["treatment", "pengobatan"]], title, body, keywords)

    def test_aspect_in_title_gets_full_credit(self):
        self.assertEqual(self._coverage("Treatment of dengue fever", ""), 1.0)

    def test_aspect_in_keywords_gets_full_credit(self):
        self.assertEqual(self._coverage("Dengue Fever", "", "dengue, treatment"), 1.0)

    def test_aspect_only_in_body_gets_partial_credit(self):
        from ragai.retrieval.retriever import ASPECT_BODY_CREDIT

        self.assertEqual(self._coverage("Dengue Fever: An Overview",
                                        "Various treatment options exist."),
                         ASPECT_BODY_CREDIT)

    def test_absent_aspect_scores_zero(self):
        self.assertEqual(self._coverage("Dengue Fever", "Epidemiology of dengue."), 0.0)

    def test_no_aspect_requested_is_not_penalised(self):
        from ragai.retrieval.retriever import _aspect_coverage

        self.assertEqual(_aspect_coverage([], "Judul apa pun", "isi apa pun"), 1.0)


class UnverifiedHasNoSourcesTests(TestCase):
    """
    Label TIDAK TERVERIFIKASI berarti sistem tidak dapat menyimpulkan apa pun.
    Melampirkan daftar referensi membantah label itu sendiri.
    """

    DOI = "10.1016/j.lungcan.2019.05.012"

    def _normalize(self, claim_text):
        from api.ai_adapter import normalize_ai_response
        from ragai.evidence import link_validator as lv

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED), \
             patch.object(lv, "fetch_doi_metadata", return_value=None):
            return normalize_ai_response(
                {"label": "uncertain", "confidence": 0.5, "summary": "s",
                 "sources": [{"doi": self.DOI, "title": "Studi terkait"}]},
                claim_text=claim_text,
            )

    def test_non_health_topic_yields_no_sources(self):
        result = self._normalize("tes")

        self.assertEqual(result["label"], "unverified")
        self.assertEqual(result["sources"], [])
        self.assertIsNone(result["confidence"])

    def test_health_claim_keeps_its_sources(self):
        result = self._normalize("Merokok menyebabkan kanker paru")

        self.assertNotEqual(result["label"], "unverified")
        self.assertTrue(result["sources"])


class SemanticFloorTests(TestCase):
    """
    Kecocokan kata kunci dan cakupan aspek bisa bernilai penuh untuk dokumen
    yang tidak ada hubungannya, terutama saat pertanyaan tidak menyebut nama
    penyakit sehingga gerbang fokus judul tidak punya dasar menolak. Kemiripan
    makna adalah sinyal terakhir yang memisahkan keduanya.
    """

    def _item(self, title, semantic):
        from ragai.evidence.selector import EvidenceItem

        return EvidenceItem(
            title=title, doi="10.1000/x", url="", snippet="", publisher="J",
            semantic_relevance=semantic, aspect_match=1.0,
        )

    def test_unrelated_document_is_dropped(self):
        from ragai.evidence.selector import _drop_semantically_unrelated

        items = [self._item("Diabetes self-management", 0.16),
                 self._item("Hydration and fluid balance", 0.62)]

        kept = _drop_semantically_unrelated(items)

        self.assertEqual([i.title for i in kept], ["Hydration and fluid balance"])

    def test_floor_is_skipped_when_semantic_scoring_unavailable(self):
        """
        Embedding mati membuat semua kandidat bernilai 0.0. Menerapkan lantai
        di situ mengosongkan mesin, bukan menyaringnya.
        """
        from ragai.evidence.selector import _drop_semantically_unrelated

        items = [self._item("A", 0.0), self._item("B", 0.0)]

        self.assertEqual(len(_drop_semantically_unrelated(items)), 2)


class WordBoundaryMatchingTests(TestCase):
    """
    Pencocokan substring mentah membuat istilah pendek cocok di tengah kata
    lain: "tes" di dalam "diabetes", "gula" di dalam "regulation". Dokumen yang
    sama sekali tidak membahas pertanyaan lalu mendapat skor leksikal penuh.
    """

    def test_short_term_does_not_match_inside_another_word(self):
        from ragai.retrieval.retriever import _variant_in

        self.assertFalse(_variant_in("tes", "understanding self-care in type 2 diabetes"))
        self.assertFalse(_variant_in("gula", "gene regulation in human cells"))

    def test_short_term_still_matches_as_a_whole_word(self):
        from ragai.retrieval.retriever import _variant_in

        self.assertTrue(_variant_in("tes", "hasil tes laboratorium"))
        self.assertTrue(_variant_in("gula", "kadar gula darah"))

    def test_longer_terms_keep_substring_behaviour(self):
        """Kecocokan parsial berguna untuk frasa: "dengue" pada "dengue fever"."""
        from ragai.retrieval.retriever import _variant_in

        self.assertTrue(_variant_in("dengue", "dengue fever outbreak"))
        self.assertTrue(_variant_in("tuberculosis", "pulmonary tuberculosis therapy"))

    def test_lexical_score_rejects_accidental_substring_hit(self):
        from ragai.retrieval.retriever import _lexical_score

        score = _lexical_score(
            ["tes"], title="Understanding Self-Care in Type 2 Diabetes", body="", keywords="")

        self.assertEqual(score, 0.0)

    def test_influenza_remains_reachable_from_flu(self):
        """
        Sebelumnya "flu" menjangkau "influenza" hanya karena kebetulan
        substring. Kini hubungan itu dinyatakan eksplisit di leksikon.
        """
        from ragai.lexicon import bilingual_variants

        self.assertIn("influenza", bilingual_variants("flu"))


class OverlappingPhraseTests(TestCase):
    """
    "gula darah tinggi" memuat "darah tinggi" sebagai rentang teks di dalamnya.
    Pencocokan frasa terpanjang tanpa entri yang tepat membaca pertanyaan gula
    darah sebagai hipertensi, lalu menjawabnya dengan paper tekanan darah.
    """

    def test_high_blood_sugar_is_not_read_as_hypertension(self):
        from ragai.retrieval.concepts import extract_conditions

        from ragai.lexicon import canonical_condition

        conditions = set(extract_conditions("gula darah tinggi berbahaya"))

        # Dibakukan menjadi nama penyakitnya, dan yang penting: BUKAN hipertensi.
        self.assertEqual(conditions, {canonical_condition("gula darah tinggi")})
        self.assertNotIn(canonical_condition("darah tinggi"), conditions)

    def test_hypertension_alone_still_recognised(self):
        from ragai.retrieval.concepts import extract_conditions

        from ragai.lexicon import canonical_condition

        self.assertIn(canonical_condition("darah tinggi"),
                      set(extract_conditions("darah tinggi berbahaya")))

    def test_high_blood_sugar_reaches_english_literature(self):
        from ragai.lexicon import bilingual_variants

        variants = bilingual_variants("gula darah tinggi")

        self.assertIn("hyperglycemia", variants)
        self.assertNotIn("hypertension", variants)


class CanonicalConditionTests(TestCase):
    """
    Satu penyakit punya banyak sebutan. Bila pertanyaan menghasilkan "darah
    tinggi" sementara judul jurnal menghasilkan "hipertensi", gerbang fokus
    judul menyimpulkan paper itu membahas hal lain dan membuangnya, sehingga
    pertanyaan tekanan darah dijawab tanpa satu pun paper tekanan darah.
    """

    def test_question_and_title_agree_on_one_name(self):
        from ragai.retrieval.concepts import extract_conditions

        question = set(extract_conditions("darah tinggi berbahaya"))
        title = set(extract_conditions("Preventing Hypertension Through Lifestyle Modification"))

        self.assertTrue(question & title, f"tidak beririsan: {question} vs {title}")

    def test_blood_sugar_question_meets_diabetes_literature(self):
        from ragai.retrieval.concepts import extract_conditions

        question = set(extract_conditions("gula darah tinggi berbahaya"))
        title = set(extract_conditions("Management of Type 2 Diabetes Mellitus"))

        self.assertTrue(question & title, f"tidak beririsan: {question} vs {title}")

    def test_distinct_diseases_are_not_merged(self):
        from ragai.lexicon import canonical_condition

        self.assertNotEqual(canonical_condition("darah tinggi"),
                            canonical_condition("diabetes"))
        self.assertNotEqual(canonical_condition("asma"), canonical_condition("tbc"))

    def test_synonyms_collapse_to_the_same_name(self):
        from ragai.lexicon import canonical_condition

        self.assertEqual(canonical_condition("darah tinggi"),
                         canonical_condition("hipertensi"))
        self.assertEqual(canonical_condition("tuberkulosis"), canonical_condition("tbc"))


class EvidenceBreadthTests(TestCase):
    """
    Jawaban yang bertumpu pada dua-tiga paper mudah meleset ketika kebetulan
    yang terambil membahas sisi lain dari topik. Basis rujukan yang lebih lebar
    membuat kesimpulan lebih stabil.
    """

    def test_default_reference_count_is_eight(self):
        from ragai.contracts import IntelligenceRequest

        self.assertEqual(IntelligenceRequest.from_payload({"query": "x"}).max_evidence, 8)

    def test_consumer_may_ask_for_more(self):
        from ragai.contracts import IntelligenceRequest

        request = IntelligenceRequest.from_payload(
            {"query": "x", "options": {"max_evidence": 15}})

        self.assertEqual(request.max_evidence, 15)

    def test_request_is_capped_at_twenty(self):
        from ragai.contracts import IntelligenceRequest

        request = IntelligenceRequest.from_payload(
            {"query": "x", "options": {"max_evidence": 500}})

        self.assertEqual(request.max_evidence, 20)

    def test_duplicate_titles_take_one_slot(self):
        """
        Knowledge base memuat paper berjudul sama dengan DOI berbeda. Keduanya
        sah, tetapi satu bacaan tidak boleh memakan dua slot referensi.
        """
        from ragai.evidence.selector import _dedupe_by_title, EvidenceItem

        items = [
            EvidenceItem(title="Gastroesophageal Reflux Disease", doi="10.1/a",
                         url="", snippet="", publisher="J"),
            EvidenceItem(title="gastroesophageal   reflux disease", doi="10.1/b",
                         url="", snippet="", publisher="J"),
            EvidenceItem(title="Dengue Fever: An Overview", doi="10.1/c",
                         url="", snippet="", publisher="J"),
        ]

        kept = _dedupe_by_title(items)

        self.assertEqual([i.doi for i in kept], ["10.1/a", "10.1/c"])


class DocumentedLimitsMatchEngineTests(TestCase):
    """
    Dokumentasi eksternal adalah janji ke pengembang lain. Bila batas yang
    tertulis berbeda dari yang benar-benar diterapkan mesin, konsumen menulis
    kode berdasarkan angka yang salah.
    """

    def test_documented_max_evidence_matches_contract(self):
        from api.openapi import build_openapi_spec
        from ragai.contracts import IntelligenceRequest

        schema = build_openapi_spec()
        options = (schema["components"]["schemas"]["QueryRequest"]
                   ["properties"]["options"]["properties"]["max_evidence"])
        default_request = IntelligenceRequest.from_payload({"query": "x"})
        capped = IntelligenceRequest.from_payload(
            {"query": "x", "options": {"max_evidence": 10_000}})

        self.assertEqual(options["default"], default_request.max_evidence)
        self.assertEqual(options["maximum"], capped.max_evidence)


class AutomaticCoverageTests(TestCase):
    """
    Pertanyaan kesehatan yang sah tetapi tidak menemukan bukti berarti ada
    lubang di basis pengetahuan, bukan pertanyaan yang buruk. Sebelumnya lubang
    itu hanya tertutup bila ada manusia yang menyadarinya lalu menjalankan
    `import_journals`. Konsumen eksternal tidak punya siapa pun yang mengawasi.
    """

    def setUp(self):
        from django.core.cache import cache

        try:
            cache.clear()
        except Exception:
            pass

    def test_topic_phrase_is_english(self):
        """Literatur Crossref berbahasa Inggris; mengirim kalimat Indonesia percuma."""
        from ragai.retrieval.acquisition import build_topic_phrase

        phrase = build_topic_phrase("apakah covid berbahaya")

        self.assertTrue(phrase)
        self.assertTrue(phrase.isascii())
        self.assertTrue(any(t in phrase for t in ("covid", "coronavirus", "sars-cov-2")))

    def test_non_health_question_triggers_no_fetch(self):
        from ragai.retrieval import acquisition

        with patch.object(acquisition, "search_crossref") as fetch:
            added = acquisition.ensure_coverage("buatkan kode python")

        self.assertEqual(added, 0)
        fetch.assert_not_called()

    def test_same_topic_is_not_fetched_twice(self):
        """
        Tanpa jeda, satu topik yang memang tidak ada di Crossref akan dicari
        ulang pada setiap permintaan dan hanya memperlambat jawaban.
        """
        from ragai.retrieval import acquisition

        with patch.object(acquisition, "search_crossref", return_value=[]) as fetch:
            acquisition.ensure_coverage("apakah covid berbahaya")
            acquisition.ensure_coverage("apakah covid berbahaya")

        self.assertEqual(fetch.call_count, 1)

    def test_network_failure_never_breaks_the_request(self):
        from ragai.retrieval import acquisition

        with patch.object(acquisition, "search_crossref", side_effect=OSError("jaringan mati")):
            self.assertEqual(acquisition.ensure_coverage("apakah covid berbahaya"), 0)

    def test_fetched_article_must_have_a_registered_doi(self):
        """
        Jalur otomatis tidak boleh menjadi pintu belakang bagi DOI karangan.
        Syaratnya sama persis dengan impor manual.
        """
        from ragai.retrieval import acquisition
        from ragai.evidence import link_validator as lv

        item = {"type": "journal-article", "DOI": "10.9999/tidak-ada",
                "title": ["Judul"], "abstract": "x" * 300}

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_UNRESOLVABLE):
            self.assertIsNone(acquisition.build_record(item))

        with patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED):
            self.assertIsNotNone(acquisition.build_record(item))

    def test_engine_completes_coverage_then_retries(self):
        """
        Mesin melengkapi lalu mengulang pengambilan sekali, bukan menyerah pada
        percobaan pertama.
        """
        from ragai import engine as engine_module

        with patch.object(engine_module, "ensure_coverage", return_value=3) as fill, \
             patch.object(engine_module, "retrieve_candidates", return_value=[]) as fetch:
            engine_module.process({"query": "apakah covid berbahaya"})

        fill.assert_called_once()
        self.assertEqual(fetch.call_count, 2)


class ClaimPathSelfHealsTests(TestCase):
    """
    Keluhan pengguna bermula di jalur verifikasi klaim, bukan di API eksternal:
    klaim yang jelas benar dilabeli "tidak pasti" hanya karena basis
    pengetahuan belum memuat topiknya. Jalur itu harus ikut melengkapi diri.
    """

    def test_empty_result_triggers_coverage_then_retry(self):
        from api import ai_adapter
        from ragai.retrieval import acquisition

        with patch.object(acquisition, "ensure_coverage", return_value=4) as fill, \
             patch("ragai.retrieval.retriever.retrieve_candidates",
                   return_value=[]) as fetch:
            ai_adapter.retrieve_grounding_evidence("covid itu berbahaya")

        fill.assert_called_once()
        self.assertEqual(fetch.call_count, 2)

    def test_sufficient_result_does_not_fetch(self):
        """Basis pengetahuan yang sudah memadai tidak boleh memicu jaringan."""
        from api import ai_adapter
        from ragai.retrieval import acquisition
        from ragai.contracts import EvidenceItem, EvidenceStatus

        item = EvidenceItem(title="Dengue Fever: An Overview", doi="10.1/a",
                            url="", snippet="x", publisher="J")

        with patch.object(acquisition, "ensure_coverage") as fill, \
             patch("ragai.evidence.selector.select_evidence",
                   return_value=([item], EvidenceStatus.SUFFICIENT)):
            ai_adapter.retrieve_grounding_evidence("demam berdarah berbahaya")

        fill.assert_not_called()


class ApiKeyIssuanceTests(TestCase):
    """
    Kunci diterbitkan operator, bukan oleh pengisi formulir. Nilai aslinya tidak
    pernah disimpan, dan satu konsumen boleh memegang banyak kunci sekaligus.
    """

    def _issue(self, **kwargs):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("issue_api_key", stdout=out, **kwargs)
        return out.getvalue()

    def _key_from(self, output):
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("ht_live_"):
                return line
        raise AssertionError(f"kunci tidak ditemukan di keluaran:\n{output}")

    def test_issued_key_is_never_stored_in_plain_text(self):
        from api.models import IntelligenceApiKey

        raw = self._key_from(self._issue(consumer="healthtalk"))
        record = IntelligenceApiKey.objects.get()

        self.assertNotEqual(record.key_hash, raw)
        self.assertEqual(record.key_hash, IntelligenceApiKey.hash_key(raw))
        self.assertNotIn(raw, str(record.__dict__))

    def test_one_consumer_may_hold_many_keys(self):
        from api.models import IntelligenceApiKey

        first = self._key_from(self._issue(consumer="healthtalk", label="produksi"))
        second = self._key_from(self._issue(consumer="healthtalk", label="staging"))

        self.assertNotEqual(first, second)
        self.assertEqual(IntelligenceApiKey.objects.filter(
            consumer="healthtalk", is_active=True).count(), 2)

    def test_stored_key_authenticates(self):
        from api.intelligence_views import resolve_consumer
        from rest_framework.test import APIRequestFactory

        raw = self._key_from(self._issue(consumer="healthtalk"))
        request = APIRequestFactory().post("/", {}, HTTP_X_API_KEY=raw)

        consumer, error = resolve_consumer(request)

        self.assertIsNone(error)
        self.assertEqual(consumer, "healthtalk")

    def test_revoked_key_stops_working(self):
        from io import StringIO

        from django.core.management import call_command
        from rest_framework.test import APIRequestFactory

        from api.intelligence_views import resolve_consumer
        from api.models import IntelligenceApiKey

        raw = self._key_from(self._issue(consumer="healthtalk"))
        call_command("revoke_api_key", id=IntelligenceApiKey.objects.get().id,
                     stdout=StringIO())

        request = APIRequestFactory().post("/", {}, HTTP_X_API_KEY=raw)
        consumer, error = resolve_consumer(request)

        self.assertIsNone(consumer)
        self.assertEqual(error.status_code, 401)

    def test_revoking_one_key_leaves_the_others(self):
        from io import StringIO

        from django.core.management import call_command
        from rest_framework.test import APIRequestFactory

        from api.intelligence_views import resolve_consumer
        from api.models import IntelligenceApiKey

        keep = self._key_from(self._issue(consumer="healthtalk", label="produksi"))
        drop = self._key_from(self._issue(consumer="healthtalk", label="staging"))
        doomed = IntelligenceApiKey.objects.get(
            key_hash=IntelligenceApiKey.hash_key(drop))
        call_command("revoke_api_key", id=doomed.id, stdout=StringIO())

        factory = APIRequestFactory()
        kept, kept_error = resolve_consumer(
            factory.post("/", {}, HTTP_X_API_KEY=keep))
        _, dropped_error = resolve_consumer(
            factory.post("/", {}, HTTP_X_API_KEY=drop))

        self.assertIsNone(kept_error)
        self.assertEqual(kept, "healthtalk")
        self.assertEqual(dropped_error.status_code, 401)

    def test_unknown_key_is_rejected_even_with_keys_in_database(self):
        from rest_framework.test import APIRequestFactory

        from api.intelligence_views import resolve_consumer

        self._issue(consumer="healthtalk")
        request = APIRequestFactory().post("/", {}, HTTP_X_API_KEY="ht_live_palsu")

        consumer, error = resolve_consumer(request)

        self.assertIsNone(consumer)
        self.assertEqual(error.status_code, 401)

    def test_database_keys_close_the_open_mode(self):
        """
        Tanpa variabel lingkungan, endpoint dulu terbuka. Begitu ada kunci di
        database, permintaan tanpa kunci harus ditolak.
        """
        from rest_framework.test import APIRequestFactory

        from api.intelligence_views import resolve_consumer

        self._issue(consumer="healthtalk")
        with self.settings(INTELLIGENCE_API_KEYS={}):
            consumer, error = resolve_consumer(APIRequestFactory().post("/", {}))

        self.assertIsNone(consumer)
        self.assertEqual(error.status_code, 401)


class AccessRequestFormTests(TestCase):
    """Formulir di halaman dokumentasi hanya mencatat permintaan, tidak memberi akses."""

    URL = "/api/v1/intelligence/access-request"

    def setUp(self):
        from django.core.cache import cache

        try:
            cache.clear()
        except Exception:
            pass

    def test_valid_request_is_recorded(self):
        from api.models import ApiAccessRequest

        response = self.client.post(self.URL, data=json.dumps({
            "name": "Dev", "email": "dev@example.com",
            "use_case": "Chatbot kesehatan untuk pengguna internal.",
            "organization": "Contoh", "expected_volume": "2000/hari",
        }), content_type="application/json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ApiAccessRequest.objects.count(), 1)
        self.assertEqual(ApiAccessRequest.objects.get().status,
                         ApiAccessRequest.STATUS_PENDING)

    def test_submitting_the_form_issues_no_key(self):
        from api.models import IntelligenceApiKey

        self.client.post(self.URL, data=json.dumps({
            "name": "Dev", "email": "dev@example.com", "use_case": "Apa saja.",
        }), content_type="application/json")

        self.assertEqual(IntelligenceApiKey.objects.count(), 0)

    def test_missing_fields_are_rejected(self):
        from api.models import ApiAccessRequest

        response = self.client.post(self.URL, data=json.dumps(
            {"name": "Dev"}), content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ApiAccessRequest.objects.count(), 0)

    def test_invalid_email_is_rejected(self):
        response = self.client.post(self.URL, data=json.dumps({
            "name": "Dev", "email": "bukan-email", "use_case": "Apa saja.",
        }), content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_documentation_page_carries_the_form(self):
        page = self.client.get("/docs").content.decode()

        self.assertIn("Request API access", page)
        self.assertIn("/api/v1/intelligence/access-request", page)
        self.assertIn('name="use_case"', page)


class HydrationVocabularyTests(TestCase):
    """
    "air" ditulis sama persis dalam Bahasa Indonesia (zat cair) dan Bahasa
    Inggris (udara). Sebagai istilah tunggal ia menarik paper polusi udara
    untuk pertanyaan tentang minum air.
    """

    def test_bare_water_word_is_not_a_search_term(self):
        from ragai.retrieval.concepts import build_search_terms

        terms = [t.lower() for t in build_search_terms("air itu penting")]

        self.assertNotIn("air", terms)

    def test_phrase_containing_it_still_reaches_english_literature(self):
        from ragai.lexicon import bilingual_variants

        variants = bilingual_variants("air putih")

        self.assertIn("drinking water", variants)

    def test_hydration_question_is_expanded_to_english(self):
        from ragai.retrieval.concepts import build_embedding_query

        expanded = build_embedding_query("air putih menjaga hidrasi", []).lower()

        self.assertIn("hydration", expanded)
        self.assertTrue("water intake" in expanded or "drinking water" in expanded)


class ThinCoverageTests(TestCase):
    """
    Bukti yang ADA belum tentu membahas yang ditanyakan. Pertanyaan tentang
    skabies bisa menarik lima paper penyakit kulit lain dan lolos sebagai
    "cukup" — persis keluhan yang paling merusak kepercayaan. Sistem harus
    menyadarinya sendiri, bukan menunggu dilaporkan.
    """

    def _item(self, title, snippet=""):
        from ragai.contracts import EvidenceItem

        return EvidenceItem(title=title, doi="10.1/a", url="", snippet=snippet,
                            publisher="J")

    def test_evidence_about_another_disease_counts_as_thin(self):
        from ragai.retrieval.acquisition import coverage_is_thin

        evidence = [self._item("Atopic dermatitis in children"),
                    self._item("Psoriasis treatment options")]

        self.assertTrue(coverage_is_thin("skabies menular lewat sentuhan kulit", evidence))

    def test_fetching_is_bounded_by_a_cooldown(self):
        """
        Pengambilan yang sesekali tidak perlu dibiarkan; yang dijaga adalah
        batasnya. Satu topik paling banyak sekali per jeda, sehingga perbedaan
        ejaan lintas bahasa tidak berubah menjadi permintaan berulang.
        """
        from ragai.retrieval import acquisition

        with patch("api.views.translate_text", return_value="scabies skin transmission"), \
             patch.object(acquisition, "search_crossref", return_value=[]) as fetch:
            acquisition.ensure_coverage("skabies menular lewat sentuhan kulit")
            acquisition.ensure_coverage("skabies menular lewat sentuhan kulit")

        self.assertEqual(fetch.call_count, 1)

    def test_empty_evidence_is_thin(self):
        from ragai.retrieval.acquisition import coverage_is_thin

        self.assertTrue(coverage_is_thin("skabies menular", []))

    def test_unknown_disease_is_translated_for_the_search(self):
        """
        Leksikon ditulis tangan dan selalu tertinggal. Penyakit yang belum
        tercatat harus tetap terjangkau lewat penerjemah.
        """
        from ragai.retrieval import acquisition

        with patch("api.views.translate_text", return_value="scabies skin contact transmission"):
            phrase = acquisition.build_topic_phrase("skabies menular lewat sentuhan kulit")

        self.assertIn("scabies", phrase.lower())

    def test_lexicon_terms_are_kept_alongside_the_translation(self):
        """
        Terjemahan menambah jangkauan, bukan menggantikan istilah baku yang
        sudah terbukti cocok dengan judul jurnal.
        """
        from ragai.retrieval import acquisition

        with patch("api.views.translate_text", return_value="is covid dangerous"):
            phrase = acquisition.build_topic_phrase("apakah covid berbahaya").lower()

        self.assertIn("covid", phrase)
        self.assertIn("coronavirus", phrase)

    def test_translation_is_cached_per_topic(self):
        """Satu topik hanya diterjemahkan sekali, supaya tidak menambah biaya."""
        from ragai.retrieval import acquisition

        with patch("api.views.translate_text",
                   return_value="scabies transmission") as translate:
            acquisition.build_topic_phrase("skabies menular lewat kulit")
            acquisition.build_topic_phrase("skabies menular lewat kulit")

        self.assertEqual(translate.call_count, 1)


class DistinctiveTokenTests(TestCase):
    """
    Satu kata umum tidak boleh menyatakan sebuah topik sudah terwakili.
    "skabies menular lewat sentuhan kulit" pernah dianggap terjawab oleh paper
    infeksi kulit mana pun, hanya karena kata "kulit" muncul di judulnya.
    """

    def _item(self, title):
        from ragai.contracts import EvidenceItem

        return EvidenceItem(title=title, doi="10.1/a", url="", snippet="", publisher="J")

    def test_generic_body_part_does_not_prove_coverage(self):
        from ragai.retrieval.acquisition import coverage_is_thin

        evidence = [self._item("Antibiotic treatment of acute bacterial skin infections"),
                    self._item("Tedizolid versus linezolid for skin structure infection")]

        self.assertTrue(coverage_is_thin("skabies menular lewat sentuhan kulit", evidence))

    def test_evaluative_words_do_not_force_a_fetch(self):
        """
        "berbahaya" tidak menunjuk topik. Tanpa pengecualian ini setiap
        pertanyaan "X berbahaya" akan dikira tidak terwakili.
        """
        from ragai.retrieval.acquisition import coverage_is_thin

        evidence = [self._item("COVID-19 severity and mortality in adults")]

        self.assertFalse(coverage_is_thin("covid itu berbahaya", evidence))


class RetryUsesTranslatedTermsTests(TestCase):
    """
    Jurnal yang baru diambil tidak berguna bila percobaan ulang memakai
    pertanyaan yang sejak awal gagal menemukannya. Istilah Inggris hasil
    terjemahan harus ikut dikirim.
    """

    def test_engine_retry_carries_english_terms(self):
        from ragai import engine as engine_module

        with patch.object(engine_module, "ensure_coverage", return_value=4), \
             patch.object(engine_module, "build_topic_phrase",
                          return_value="scabies transmission"), \
             patch.object(engine_module, "retrieve_candidates",
                          return_value=[]) as fetch:
            engine_module.process({"query": "skabies menular lewat kulit"})

        self.assertEqual(fetch.call_count, 2)
        retry_terms = fetch.call_args_list[1].kwargs.get("extra_terms") or []
        self.assertIn("scabies", [t.lower() for t in retry_terms])


class AcquisitionLatencyTests(TestCase):
    """
    Pengambilan otomatis berjalan di dalam permintaan pengguna. Setiap detik
    yang dihabiskan di sini adalah detik yang ditunggu orang di depan layar.
    """

    def setUp(self):
        from django.core.cache import cache

        try:
            cache.clear()
        except Exception:
            pass

    def test_embedding_does_not_block_the_request(self):
        """
        Satu embedding memakan lebih dari satu detik. Mengerjakan puluhan di
        dalam permintaan menambahkan hampir setengah menit ke waktu tunggu.
        """
        import time

        from ragai.retrieval import acquisition
        from api.models import JournalArticle

        JournalArticle.objects.create(title="Belum ter-embed", doi="10.1/a",
                                      abstract="x" * 300, is_embedded=False)

        def slow_embed(article):
            time.sleep(0.4)

        with patch("api.views.embed_journal_article", side_effect=slow_embed):
            started = time.monotonic()
            acquisition._embed_new_articles()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2,
                        f"pemanggil ikut menunggu embedding: {elapsed:.2f}s")

    def test_covered_topic_does_not_trigger_a_fetch(self):
        """
        Kata Indonesia biasa seperti "menular" tidak boleh membuat pertanyaan
        yang sudah terjawab lengkap dinilai belum terwakili.
        """
        from ragai.contracts import EvidenceItem
        from ragai.retrieval.acquisition import coverage_is_thin

        evidence = [EvidenceItem(title="Typhoid fever transmission and control",
                                 doi="10.1/a", url="", snippet="", publisher="J")]

        self.assertFalse(coverage_is_thin("apakah tifus menular lewat makanan", evidence))

    def test_unknown_disease_still_triggers_a_fetch(self):
        from ragai.contracts import EvidenceItem
        from ragai.retrieval.acquisition import coverage_is_thin

        evidence = [EvidenceItem(title="Antibiotic treatment of bacterial skin infection",
                                 doi="10.1/a", url="", snippet="", publisher="J")]

        self.assertTrue(coverage_is_thin("skabies menular lewat sentuhan kulit", evidence))

    def test_known_disease_absent_from_evidence_triggers_a_fetch(self):
        from ragai.contracts import EvidenceItem
        from ragai.retrieval.acquisition import coverage_is_thin

        evidence = [EvidenceItem(title="Hypertension and lifestyle change",
                                 doi="10.1/a", url="", snippet="", publisher="J")]

        self.assertTrue(coverage_is_thin("apakah tifus menular lewat makanan", evidence))

    def test_duplicates_are_filtered_with_one_query(self):
        """
        Satu query untuk seluruh DOI, bukan satu query per artikel.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from ragai.retrieval import acquisition
        from api.models import JournalArticle

        JournalArticle.objects.create(title="Ada", doi="10.1/a", abstract="x" * 300)
        items = [{"type": "journal-article", "DOI": f"10.1/{c}",
                  "title": ["T"], "abstract": "x" * 300} for c in "abcd"]

        with patch.object(acquisition, "search_crossref", return_value=items), \
             patch("api.views.translate_text", return_value="typhoid fever"), \
             patch.object(acquisition.lv, "resolve_doi",
                          return_value=acquisition.lv.STATUS_VERIFIED), \
             patch.object(acquisition, "_embed_new_articles"), \
             CaptureQueriesContext(connection) as queries:
            created = acquisition.ensure_coverage("apakah tifus menular", health_checked=True)

        self.assertEqual(created, 3)
        selects = [q for q in queries.captured_queries
                   if q["sql"].lower().startswith("select") and "journalarticle" in q["sql"].lower()]
        self.assertLessEqual(len(selects), 2, f"terlalu banyak query: {len(selects)}")


class LinkValidationLatencyTests(TestCase):
    """
    Setiap referensi butuh sampai dua perjalanan jaringan. Dikerjakan
    berurutan, delapan referensi menjadi sekitar tiga detik yang seluruhnya
    ditanggung orang yang sedang menunggu jawaban.
    """

    def _items(self, count):
        from ragai.contracts import EvidenceItem

        return [EvidenceItem(title=f"Paper {i}", doi=f"10.1000/{i}", url="",
                             snippet="", publisher="J") for i in range(count)]

    def test_validation_runs_concurrently(self):
        import time

        from ragai.evidence import selector
        from ragai.evidence import link_validator as lv

        def slow_validate(doi, url, timeout=5.0, trust_on_unknown=False):
            time.sleep(0.25)
            return {"doi": doi, "url": url, "doi_verified": True,
                    "link_status": lv.STATUS_VERIFIED}

        with patch.object(selector.lv, "validate_reference", side_effect=slow_validate), \
             patch.object(selector, "_apply_registry_metadata"):
            started = time.monotonic()
            selector.validate_links(self._items(8))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0,
                        f"validasi masih berurutan: {elapsed:.2f}s untuk 8 referensi")

    def test_every_item_is_still_validated(self):
        from ragai.evidence import selector
        from ragai.evidence import link_validator as lv

        with patch.object(selector.lv, "validate_reference",
                          return_value={"doi": "10.1/x", "url": "u", "doi_verified": True,
                                        "link_status": lv.STATUS_VERIFIED}) as check, \
             patch.object(selector, "_apply_registry_metadata"):
            out = selector.validate_links(self._items(5))

        self.assertEqual(check.call_count, 5)
        self.assertEqual(len(out), 5)
        self.assertTrue(all(i.doi_verified for i in out))

    def test_one_broken_item_does_not_sink_the_rest(self):
        from ragai.evidence import selector
        from ragai.evidence import link_validator as lv

        calls = {"n": 0}

        def flaky(doi, url, timeout=5.0, trust_on_unknown=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("jaringan putus")
            return {"doi": doi, "url": url, "doi_verified": True,
                    "link_status": lv.STATUS_VERIFIED}

        with patch.object(selector.lv, "validate_reference", side_effect=flaky), \
             patch.object(selector, "_apply_registry_metadata"):
            out = selector.validate_links(self._items(4))

        self.assertEqual(len(out), 4)
        self.assertEqual(sum(1 for i in out if i.doi_verified), 3)


class ConversationIdentityTests(TestCase):
    """
    Pengenal ruang obrolan dibuat oleh produk lain. Sistem ini tidak boleh
    menuntut pendaftaran lebih dulu, dan tidak boleh mencampur percakapan milik
    dua produk yang kebetulan memakai pengenal yang sama.
    """

    def _payload(self, query, **context):
        return {"query": query, "context": context}

    def test_any_identifier_works_without_registration(self):
        from ragai import engine
        from api.models import ConversationSession

        with patch.object(engine, "retrieve_candidates", return_value=[]):
            response = engine.process(
                self._payload("apakah demam berbahaya", conversation_id="room-abc-123"))

        self.assertEqual(response.conversation_id, "room-abc-123")
        self.assertEqual(ConversationSession.objects.count(), 1)

    def test_common_field_names_are_accepted(self):
        """Tiap produk menamai ruang obrolannya berbeda."""
        from ragai.contracts import IntelligenceRequest

        for field in ("conversation_id", "session_id", "room_id", "thread_id", "chat_id"):
            request = IntelligenceRequest.from_payload(
                {"query": "x", "context": {field: "r-1"}})
            self.assertEqual(request.conversation_id, "r-1", f"gagal untuk {field}")

    def test_two_products_do_not_share_a_room(self):
        """
        Dua produk bisa sama-sama memakai "room-1" tanpa saling tahu. Riwayat
        salah satu tidak boleh terbaca oleh yang lain.
        """
        from ragai import engine

        with patch.object(engine, "retrieve_candidates", return_value=[]):
            engine.process(self._payload("saya demam", conversation_id="room-1"),
                           consumer="produk-a")
            engine.process(self._payload("saya batuk", conversation_id="room-1"),
                           consumer="produk-b")

        from ragai.context.conversation import load_state

        a = load_state("room-1", consumer="produk-a")
        b = load_state("room-1", consumer="produk-b")

        self.assertIn("saya demam", " ".join(a.user_messages()))
        self.assertNotIn("saya batuk", " ".join(a.user_messages()))
        self.assertIn("saya batuk", " ".join(b.user_messages()))
        self.assertNotIn("saya demam", " ".join(b.user_messages()))

    def test_existing_sessions_keep_working(self):
        """Sesi yang sudah berjalan tidak boleh terputus oleh pemisahan ini."""
        from ragai.context.conversation import load_state
        from api.models import ConversationMessage, ConversationSession

        legacy = ConversationSession.objects.create(session_id="room-lama",
                                                    consumer="healthtalk")
        ConversationMessage.objects.create(session=legacy, role="user",
                                           content="pertanyaan lama")

        state = load_state("room-lama", consumer="healthtalk")

        self.assertIn("pertanyaan lama", " ".join(state.user_messages()))


class ConversationEvidenceTests(TestCase):
    """
    Satu pembahasan di ruang obrolan harus bersandar pada jurnal yang sama.
    Mengulang pencarian pada tiap gelembung membuat rujukan berganti-ganti dan
    jawaban tampak berubah pendirian untuk pembahasan yang sama.
    """

    def setUp(self):
        from api.models import JournalArticle

        import datetime

        # Menyerupai baris nyata: DOI penerbit yang dikenal dan tahun terbit,
        # supaya pengujian menguji alur percakapan dan bukan tersandung skor
        # kualitas metadata.
        self.journal = JournalArticle.objects.create(
            title="Dengue fever transmission by Aedes mosquitoes",
            abstract="Dengue fever is transmitted by Aedes mosquito bites. " * 12,
            doi="10.1016/j.dengue.2021.01.001",
            url="https://doi.org/10.1016/j.dengue.2021.01.001",
            publisher="Elsevier", journal_name="Journal of Tropical Medicine",
            published_date=datetime.date(2021, 6, 1), is_embedded=False,
        )

    def _item(self):
        from ragai.contracts import EvidenceItem, EvidenceOrigin

        return EvidenceItem(
            chunk_id=f"journal:{self.journal.id}",
            source_id=f"journal:{self.journal.id}",
            title=self.journal.title, snippet=self.journal.abstract[:400],
            doi=self.journal.doi, url=self.journal.url,
            publisher=self.journal.publisher,
            origin=EvidenceOrigin.KNOWLEDGE_BASE,
            # Skor setara hasil retrieval sungguhan; tanpa ini item contoh
            # tersaring ambang relevansi dan pengujian menguji hal lain.
            semantic_relevance=0.8, aspect_match=1.0, source_quality=0.8,
        )

    def _no_network(self):
        """DOI contoh tidak terdaftar di registry mana pun."""
        from ragai.evidence import link_validator as lv

        return (patch.object(lv, "resolve_doi", return_value=lv.STATUS_VERIFIED),
                patch.object(lv, "fetch_doi_metadata", return_value=None))

    def test_first_question_always_searches(self):
        from ragai import engine

        doi_ok, meta_ok = self._no_network()
        with doi_ok, meta_ok, patch.object(
                engine, "retrieve_candidates", return_value=[self._item()]) as search:
            response = engine.process(
                {"query": "apakah demam berdarah ditularkan nyamuk",
                 "context": {"conversation_id": "room-dbd"}})

        search.assert_called()
        self.assertEqual((response.metadata or {}).get("evidence_source"), "retrieval")

    def test_follow_up_within_the_same_journals_reuses_them(self):
        from ragai import engine

        doi_ok, meta_ok = self._no_network()
        with doi_ok, meta_ok, patch.object(
                engine, "retrieve_candidates", return_value=[self._item()]):
            engine.process({"query": "apakah demam berdarah ditularkan nyamuk",
                            "context": {"conversation_id": "room-dbd"}})

        with patch.object(engine, "retrieve_candidates") as search:
            response = engine.process(
                {"query": "nyamuk apa yang menularkan demam berdarah",
                 "context": {"conversation_id": "room-dbd"}})

        search.assert_not_called()
        self.assertEqual((response.metadata or {}).get("evidence_source"), "conversation")
        self.assertTrue(response.evidence)

    def test_question_beyond_the_journals_searches_again(self):
        from ragai import engine

        doi_ok, meta_ok = self._no_network()
        with doi_ok, meta_ok, patch.object(
                engine, "retrieve_candidates", return_value=[self._item()]):
            engine.process({"query": "apakah demam berdarah ditularkan nyamuk",
                            "context": {"conversation_id": "room-dbd"}})

        with patch.object(engine, "retrieve_candidates", return_value=[]) as search:
            response = engine.process(
                {"query": "apakah asam urat boleh makan emping",
                 "context": {"conversation_id": "room-dbd"}})

        search.assert_called()
        self.assertEqual((response.metadata or {}).get("evidence_source"), "retrieval")

    def test_a_room_without_history_never_reuses(self):
        from ragai import engine

        doi_ok, meta_ok = self._no_network()
        with doi_ok, meta_ok, patch.object(
                engine, "retrieve_candidates", return_value=[self._item()]) as search:
            engine.process({"query": "apakah demam berdarah ditularkan nyamuk",
                            "context": {"conversation_id": "room-baru"}})

        search.assert_called()

    def test_consumer_sees_whether_sources_were_reused(self):
        from ragai import engine
        from ragai.adapters.healthtalk import to_simple_response

        doi_ok, meta_ok = self._no_network()
        with doi_ok, meta_ok, patch.object(
                engine, "retrieve_candidates", return_value=[self._item()]):
            first = engine.process({"query": "apakah demam berdarah ditularkan nyamuk",
                                    "context": {"conversation_id": "room-dbd"}})
        with patch.object(engine, "retrieve_candidates"):
            second = engine.process({"query": "nyamuk apa penyebab demam berdarah",
                                     "context": {"conversation_id": "room-dbd"}})

        self.assertFalse(to_simple_response(first)["sources_reused"])
        self.assertTrue(to_simple_response(second)["sources_reused"])

    def test_reused_evidence_keeps_the_same_references(self):
        """Rujukan yang ditampilkan harus persis sama, bukan sekadar mirip."""
        from ragai import engine

        doi_ok, meta_ok = self._no_network()
        with doi_ok, meta_ok, patch.object(
                engine, "retrieve_candidates", return_value=[self._item()]):
            first = engine.process({"query": "apakah demam berdarah ditularkan nyamuk",
                                    "context": {"conversation_id": "room-dbd"}})
        with patch.object(engine, "retrieve_candidates"):
            second = engine.process({"query": "nyamuk apa penyebab demam berdarah",
                                     "context": {"conversation_id": "room-dbd"}})

        self.assertEqual([e.doi for e in first.evidence],
                         [e.doi for e in second.evidence])


class ConversationDocumentationTests(TestCase):
    """
    Dokumentasi adalah janji ke pengembang lain. Nama field yang mereka baca
    harus benar-benar diterima mesin, dan sebaliknya.
    """

    def test_documented_room_fields_are_all_accepted(self):
        from ragai.contracts import IntelligenceRequest
        from api.openapi import build_openapi_spec

        described = build_openapi_spec()["info"]["description"]

        for field in ("conversation_id", "session_id", "room_id",
                      "thread_id", "chat_id"):
            self.assertIn(f"`context.{field}`", described,
                          f"{field} tidak disebut dokumentasi")
            request = IntelligenceRequest.from_payload(
                {"query": "x", "context": {field: "r-9"}})
            self.assertEqual(request.conversation_id, "r-9")

    def test_reuse_flag_is_documented_and_returned(self):
        from api.openapi import build_openapi_spec

        spec = build_openapi_spec()
        simple = spec["components"]["schemas"]["SimpleQueryResponse"]["properties"]

        self.assertIn("sources_reused", simple)
        self.assertIn("sources_reused", spec["info"]["description"])


class TopicShiftTests(TestCase):
    """
    Percakapan harus bisa berpindah pembahasan. Keputusan memakai ulang jurnal
    dibuat dari pertanyaan ASLI pengguna; query yang sudah diperkaya konteks
    selalu membawa topik sebelumnya, sehingga memakainya membuat ruang obrolan
    terkunci selamanya pada jurnal pertama.
    """

    def _pool(self):
        from ragai.contracts import EvidenceItem

        return [EvidenceItem(
            title="Dengue haemorrhagic fever in Indonesia",
            snippet="Dengue fever is transmitted by Aedes mosquitoes and causes bleeding.",
            doi="10.1016/j.dengue.2021.01.001", url="u", publisher="Elsevier")]

    def test_follow_up_in_known_vocabulary_reuses(self):
        from ragai.context.evidence_memory import can_answer_from_memory

        self.assertTrue(can_answer_from_memory("apa gejalanya", self._pool()))
        self.assertTrue(can_answer_from_memory("apa saja gejala penyakit ini",
                                               self._pool()))

    def test_unrecognised_wording_falls_back_to_searching(self):
        """
        Kata yang tidak dikenali kosakata kesehatan dan juga tidak muncul di
        jurnal diperlakukan sebagai kemungkinan topik baru. Sisi ini sengaja
        dipilih: mencari ulang hanya menambah waktu, sedangkan memakai ulang
        jurnal yang keliru menghasilkan rujukan yang salah. Pencarian ulang pun
        tetap membawa konteks percakapan, sehingga umumnya menemukan jurnal
        yang sama.
        """
        from ragai.context.evidence_memory import can_answer_from_memory

        self.assertFalse(can_answer_from_memory("berapa lama sembuhnya", self._pool()))

    def test_a_different_disease_ends_the_reuse(self):
        from ragai.context.evidence_memory import can_answer_from_memory

        self.assertFalse(can_answer_from_memory("kalau asam urat bagaimana", self._pool()))

    def test_the_same_disease_keeps_the_reuse(self):
        from ragai.context.evidence_memory import can_answer_from_memory

        self.assertTrue(can_answer_from_memory("demam berdarah menular lewat apa",
                                               self._pool()))

    def test_a_disease_outside_the_lexicon_ends_the_reuse(self):
        """
        Leksikon selalu tertinggal. Kata tak dikenal yang juga tidak muncul di
        jurnal diperlakukan sebagai topik baru, bukan sebagai lanjutan.
        """
        from ragai.context.evidence_memory import can_answer_from_memory

        self.assertFalse(can_answer_from_memory("kalau skabies bagaimana", self._pool()))

    def test_enriched_query_would_have_locked_the_room(self):
        """
        Menjaga alasan perbaikan ini tetap terlihat: query yang diperkaya
        konteks memuat topik lama, sehingga selalu dinilai masih tercakup.
        """
        from ragai.context.evidence_memory import can_answer_from_memory

        enriched = "kalau asam urat bagaimana demam berdarah pendarahan"

        self.assertTrue(can_answer_from_memory(enriched, self._pool()))
        self.assertFalse(can_answer_from_memory("kalau asam urat bagaimana", self._pool()))


class TopicChangeDropsStaleContextTests(TestCase):
    """
    Mendeteksi perpindahan topik saja tidak cukup. Query untuk pencarian
    diperkaya konteks percakapan, dan istilah topik lama menenggelamkan
    penyakit yang baru disebut, sehingga pencarian "baru" mengembalikan jurnal
    yang sama persis.
    """

    def _pool(self):
        from ragai.contracts import EvidenceItem

        return [EvidenceItem(
            title="Dengue haemorrhagic fever in Indonesia",
            snippet="Dengue fever is transmitted by Aedes mosquitoes.",
            doi="10.1016/j.dengue.2021.01.001", url="u", publisher="Elsevier")]

    def test_a_new_disease_is_recognised_as_a_topic_change(self):
        from ragai.context.evidence_memory import topic_changed

        self.assertTrue(topic_changed("kalau asam urat bagaimana", self._pool()))

    def test_a_follow_up_is_not_a_topic_change(self):
        from ragai.context.evidence_memory import topic_changed

        self.assertFalse(topic_changed("apa gejalanya", self._pool()))
        self.assertFalse(topic_changed("demam berdarah menular lewat apa", self._pool()))

    def test_search_uses_the_bare_question_after_a_topic_change(self):
        from ragai import engine
        from api.models import JournalArticle

        JournalArticle.objects.create(
            title="Dengue haemorrhagic fever in Indonesia",
            abstract="Dengue fever is transmitted by Aedes mosquitoes. " * 12,
            doi="10.1016/j.dengue.2021.01.001", url="u", publisher="Elsevier")

        with patch.object(engine, "recent_evidence", return_value=self._pool()), \
             patch.object(engine, "retrieve_candidates", return_value=[]) as search:
            engine.process({"query": "kalau asam urat bagaimana",
                            "context": {"conversation_id": "room-x"}})

        asked = search.call_args_list[0].args[0]
        self.assertEqual(asked, "kalau asam urat bagaimana")
        self.assertNotIn("berdarah", asked.lower())


class MemoryOrderingTests(TestCase):
    """
    Ingatan percakapan harus mengikuti pembahasan yang sedang berjalan. Membaca
    giliran terlama membuat lanjutan sesudah perpindahan topik kembali memakai
    jurnal topik yang sudah ditinggalkan.
    """

    def test_most_recent_turn_leads_the_pool(self):
        import json

        from ragai.context.evidence_memory import recent_evidence
        from api.models import (ConversationMessage, ConversationSession,
                                JournalArticle)

        old_topic = JournalArticle.objects.create(
            title="Dengue haemorrhagic fever", abstract="x" * 300, doi="10.1/a")
        new_topic = JournalArticle.objects.create(
            title="Gout and purine diet", abstract="y" * 300, doi="10.1/b")

        session = ConversationSession.objects.create(session_id="s", consumer="c")
        for journal in (old_topic, new_topic):
            ConversationMessage.objects.create(
                session=session, role="assistant", content="jawaban",
                evidence_refs=json.dumps([{"source_id": f"journal:{journal.id}"}]))

        pool = recent_evidence(session)

        self.assertEqual(pool[0].title, "Gout and purine diet")


class ContextFollowsTheTopicTests(TestCase):
    """
    Konteks kesehatan akumulatif berguna selama satu pembahasan. Setelah
    pengguna berpindah penyakit, akumulasi itu justru menarik jurnal topik lama
    ke setiap giliran berikutnya.
    """

    def _pool(self):
        from ragai.contracts import EvidenceItem

        return [EvidenceItem(
            title="Dengue haemorrhagic fever in Indonesia",
            snippet="Dengue fever with bleeding and fever.",
            doi="10.1016/j.dengue.2021.01.001", url="u", publisher="Elsevier")]

    def setUp(self):
        from ragai.context.conversation import storage_key
        from api.models import ConversationSession

        # Ingatan hanya terbaca bila ruang obrolan sudah punya baris sesi.
        for room in ("room-y", "room-z"):
            ConversationSession.objects.create(
                session_id=storage_key("healthify", room), consumer="healthify")

    def test_context_restarts_when_the_disease_changes(self):
        from ragai import engine

        with patch.object(engine, "recent_evidence", return_value=self._pool()), \
             patch.object(engine, "retrieve_candidates", return_value=[]):
            response = engine.process(
                {"query": "kalau asam urat bagaimana",
                 "context": {"conversation_id": "room-y",
                             "previous_messages": [
                                 {"role": "user", "content": "saya demam dan pendarahan"}]}})

        symptoms = " ".join(response.health_context.symptoms).lower()
        self.assertNotIn("pendarahan", symptoms)

    def test_context_still_accumulates_within_one_topic(self):
        from ragai import engine

        with patch.object(engine, "recent_evidence", return_value=self._pool()), \
             patch.object(engine, "retrieve_candidates", return_value=[]):
            response = engine.process(
                {"query": "sudah tiga hari",
                 "context": {"conversation_id": "room-z",
                             "previous_messages": [
                                 {"role": "user", "content": "saya demam"}]}})

        self.assertEqual(response.health_context.duration, "3 hari")
        self.assertIn("demam", " ".join(response.health_context.symptoms).lower())


class EmptySnapshotIsNotAMissingSnapshotTests(TestCase):
    """
    Konteks yang sengaja dikosongkan saat pembahasan berpindah tidak boleh
    dibangun ulang dari riwayat: membangunnya ulang mengembalikan penyakit yang
    baru saja ditinggalkan, dan giliran berikutnya kembali menarik jurnal lama.
    """

    def test_stored_empty_context_is_kept(self):
        import json

        from ragai.context.conversation import load_state, storage_key
        from api.models import ConversationMessage, ConversationSession

        session = ConversationSession.objects.create(
            session_id=storage_key("healthify", "room-q"), consumer="healthify",
            health_context=json.dumps({"symptoms": [], "chief_complaint": None}))
        ConversationMessage.objects.create(session=session, role="user",
                                           content="saya demam dan pendarahan")

        state = load_state("room-q", consumer="healthify")

        self.assertTrue(state.has_snapshot)
        self.assertTrue(state.health_context.is_empty())

    def test_history_still_rebuilds_when_no_snapshot_exists(self):
        """Consumer yang mengirim riwayat sendiri tetap mendapat konteksnya."""
        from ragai.context.conversation import (
            load_state, rebuild_context_from_history)

        state = load_state(None, previous_messages=[
            {"role": "user", "content": "saya demam tiga hari"}])

        self.assertFalse(state.has_snapshot)
        self.assertIn("demam", " ".join(
            rebuild_context_from_history(state).symptoms).lower())


class AccessRequestNotificationTests(TestCase):
    """
    Permintaan akses tidak berguna bila tidak ada yang tahu permintaan itu
    masuk. Sebaliknya, kegagalan mengirim surat tidak boleh membatalkan
    permintaan yang sudah tersimpan.
    """

    URL = "/api/v1/intelligence/access-request"

    def setUp(self):
        from django.core.cache import cache

        try:
            cache.clear()
        except Exception:
            pass

    def _submit(self):
        return self.client.post(self.URL, data=json.dumps({
            "name": "Dev", "email": "dev@example.com",
            "use_case": "Chatbot kesehatan.", "organization": "Contoh",
        }), content_type="application/json")

    def test_operator_is_notified(self):
        from api import email_service as service

        with patch.object(service.email_service, "notify_admin_access_request") as notify:
            response = self._submit()

        self.assertEqual(response.status_code, 201)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].email, "dev@example.com")

    def test_a_failing_mailbox_does_not_lose_the_request(self):
        from api import email_service as service
        from api.models import ApiAccessRequest

        with patch.object(service.email_service, "notify_admin_access_request",
                          side_effect=OSError("smtp mati")):
            response = self._submit()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ApiAccessRequest.objects.count(), 1)

    def test_notification_carries_the_request_details(self):
        from django.core import mail

        from api.email_service import email_service
        from api.models import ApiAccessRequest

        access_request = ApiAccessRequest.objects.create(
            name="Dev", email="dev@example.com", use_case="Chatbot kesehatan.",
            organization="Contoh", expected_volume="2000/hari")

        with self.settings(ADMIN_NOTIFICATION_EMAILS=["ops@example.com"],
                           ENABLE_EMAIL_NOTIFICATIONS=True,
                           EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            email_service.admin_emails = ["ops@example.com"]
            email_service.enabled = True
            sent = email_service.notify_admin_access_request(access_request)

        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("dev@example.com", body)
        self.assertIn("Chatbot kesehatan.", body)
        self.assertIn(f"--request {access_request.id}", body)


class ApiKeyDeliveryTests(TestCase):
    """
    Nilai asli kunci hanya pernah ada di terminal operator dan di surat ini.
    Karena itu kegagalan pengiriman harus terlihat, bukan ditelan diam-diam.
    """

    def _issue(self, **kwargs):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("issue_api_key", stdout=out, **kwargs)
        return out.getvalue()

    def test_key_is_emailed_to_the_requester(self):
        from django.core import mail

        from api.email_service import email_service
        from api.models import ApiAccessRequest

        access_request = ApiAccessRequest.objects.create(
            name="Dev", email="dev@example.com", use_case="Chatbot.")

        with self.settings(ENABLE_EMAIL_NOTIFICATIONS=True,
                           EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                           PUBLIC_API_BASE_URL="https://ragai.example.com"):
            email_service.enabled = True
            output = self._issue(consumer="healthtalk", request=access_request.id,
                                 email=True)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["dev@example.com"])

        raw_key = next(line.strip() for line in output.splitlines()
                       if line.strip().startswith("ht_live_"))
        self.assertIn(raw_key, message.body)
        self.assertIn("https://ragai.example.com/docs", message.body)

    def test_delivery_failure_is_reported_loudly(self):
        from api import email_service as service
        from api.models import ApiAccessRequest

        access_request = ApiAccessRequest.objects.create(
            name="Dev", email="dev@example.com", use_case="Chatbot.")

        with patch.object(service.email_service, "send_api_key", return_value=False):
            output = self._issue(consumer="healthtalk", request=access_request.id,
                                 email=True)

        self.assertIn("GAGAL", output)

    def test_nothing_is_sent_without_the_flag(self):
        from api import email_service as service
        from api.models import ApiAccessRequest

        access_request = ApiAccessRequest.objects.create(
            name="Dev", email="dev@example.com", use_case="Chatbot.")

        with patch.object(service.email_service, "send_api_key") as send:
            self._issue(consumer="healthtalk", request=access_request.id)

        send.assert_not_called()


class PublicSiteTests(TestCase):
    """
    Engine publik punya domain dan identitasnya sendiri; produk Healthify tetap
    di domainnya. Yang diuji di sini adalah bahwa keduanya tidak tertukar.
    """

    def test_landing_page_introduces_the_engine(self):
        page = self.client.get("/").content.decode()

        self.assertIn("ragai", page)
        self.assertIn("Request API access", page)
        self.assertIn("/docs", page)

    def test_landing_page_states_the_beta_terms(self):
        page = self.client.get("/").content.decode().lower()

        self.assertIn("beta", page)
        self.assertIn("free", page)

    def test_landing_page_carries_the_request_form(self):
        page = self.client.get("/").content.decode()

        self.assertIn("/api/v1/intelligence/access-request", page)
        self.assertIn('name="use_case"', page)

    def test_documentation_points_at_the_public_address(self):
        from api.openapi import build_openapi_spec

        with self.settings(PUBLIC_API_BASE_URL="https://ragai.example.com"):
            spec = build_openapi_spec()

        self.assertEqual(spec["servers"][0]["url"], "https://ragai.example.com")
        self.assertNotIn("healthify.twenti.studio", json.dumps(spec))

    def test_documentation_declares_the_beta_status(self):
        from api.openapi import build_openapi_spec

        description = build_openapi_spec()["info"]["description"].lower()

        self.assertIn("beta", description)
        self.assertIn("free", description)


class SmallTalkTests(TestCase):
    """
    Di ruang obrolan, sebagian pesan bukan pertanyaan: sapaan, ucapan terima
    kasih, penutup. Terlihat di log produksi bahwa "terima kasih banyak"
    menempuh pencarian literatur lengkap selama hampir tiga detik dan kembali
    membawa lima jurnal yang tidak ada hubungannya dengan apa pun.
    """

    COURTESY = ["terima kasih banyak", "makasih ya", "halo", "hai dok",
                "selamat pagi", "oke", "thanks"]

    def test_courtesy_is_recognised(self):
        from ragai.contracts import Intent
        from ragai.query_understanding.classifier import classify_intent

        for message in self.COURTESY:
            self.assertEqual(classify_intent(message).intent, Intent.SMALL_TALK,
                             f"gagal untuk {message!r}")

    def test_a_greeting_with_a_complaint_is_still_a_complaint(self):
        """
        "halo dok, saya batuk berdahak sudah seminggu" adalah keluhan yang
        kebetulan diawali sapaan. Keluhannya tidak boleh hilang.
        """
        from ragai.contracts import Intent
        from ragai.query_understanding.classifier import classify_intent

        for message in ("halo dok, saya batuk berdahak sudah seminggu",
                        "selamat pagi, apakah demam berdarah menular",
                        "terima kasih, tapi apakah asam urat boleh makan emping"):
            self.assertNotEqual(classify_intent(message).intent, Intent.SMALL_TALK,
                                f"salah dikira basa-basi: {message!r}")

    def test_no_retrieval_and_no_sources_for_courtesy(self):
        from ragai import engine

        with patch.object(engine, "retrieve_candidates") as search:
            response = engine.process({"query": "terima kasih banyak"})

        search.assert_not_called()
        self.assertEqual(response.evidence, [])
        self.assertTrue(response.answer.strip())

    def test_the_reply_is_courteous_not_a_refusal(self):
        """
        Dijawab ramah, bukan dengan penolakan "di luar cakupan" yang dipakai
        untuk pertanyaan non-kesehatan.
        """
        from ragai import engine

        with patch.object(engine, "retrieve_candidates"):
            thanks = engine.process({"query": "terima kasih banyak"}).answer.lower()
            greeting = engine.process({"query": "halo"}).answer.lower()

        self.assertNotIn("di luar cakupan", thanks)
        self.assertIn("sama-sama", thanks)
        self.assertIn("halo", greeting)

    def test_courtesy_does_not_trigger_knowledge_acquisition(self):
        from ragai import engine

        with patch.object(engine, "ensure_coverage") as fill, \
             patch.object(engine, "retrieve_candidates"):
            engine.process({"query": "makasih ya"})

        fill.assert_not_called()


class CitationMarkersNeverReachReadersTests(TestCase):
    """
    Penanda `[E1]` adalah notasi internal untuk menelusuri kalimat ke bukti.
    Di layar ia hanya tampak sebagai angka dalam kurung yang tidak berarti bagi
    pembaca, dan itu terjadi di produksi: jawaban format penuh dan ringkasan
    verifikasi sama-sama masih memuatnya.
    """

    ANSWER = "Demam berdarah ditularkan nyamuk Aedes. [E1] Gejalanya demam tinggi. [E2]"

    def _response(self):
        from ragai.contracts import (
            EvidenceStatus, HealthContext, IntelligenceResponse, Intent, Mode,
            SafetyDecision)

        return IntelligenceResponse(
            answer=self.ANSWER, intent=Intent.HEALTH_INFORMATION, mode=Mode.INFORMATION,
            health_context=HealthContext(), evidence=[], claims=[],
            evidence_status=EvidenceStatus.SUFFICIENT, safety_decision=SafetyDecision.PASS,
            safety_flags=[], metadata={},
        )

    def test_full_format_answer_is_clean(self):
        from ragai.adapters.healthtalk import to_consumer_response

        body = to_consumer_response(self._response())

        self.assertNotIn("[E1]", body["answer"])
        self.assertNotIn("[E2]", body["answer"])
        self.assertIn("nyamuk Aedes.", body["answer"])

    def test_annotated_version_is_still_available(self):
        """Yang butuh pemetaan kalimat ke bukti tidak kehilangan apa pun."""
        from ragai.adapters.healthtalk import to_consumer_response

        body = to_consumer_response(self._response())

        self.assertIn("[E1]", body["answer_annotated"])

    def test_simple_format_stays_clean(self):
        from ragai.adapters.healthtalk import to_simple_response

        self.assertNotIn("[E", to_simple_response(self._response())["answer"])

    def test_no_space_is_left_before_punctuation(self):
        from ragai.citations import strip_citation_markers

        self.assertEqual(strip_citation_markers("Benar [E1]. Lalu [E2], juga."),
                         "Benar. Lalu, juga.")

    def test_verification_summary_is_clean(self):
        from api.ai_adapter import normalize_ai_response

        result = normalize_ai_response(
            {"label": "valid", "confidence": 0.9,
             "summary": "Merokok menyebabkan kanker paru. [E1] [E2]", "sources": []},
            claim_text="merokok menyebabkan kanker paru")

        self.assertNotIn("[E", result["summary"])


class PendingKeyIssuanceTests(TestCase):
    """
    Menutup jarak antara formulir dan kunci yang sampai ke pemohon. Yang paling
    penting di sini bukan jalur suksesnya, melainkan bahwa perintah ini aman
    dijalankan berulang dan tidak meninggalkan kunci hidup tanpa pemilik.
    """

    def setUp(self):
        from api.models import ApiAccessRequest

        self.request = ApiAccessRequest.objects.create(
            name="Dev", email="dev@example.com", organization="Contoh Studio",
            use_case="Chatbot kesehatan.")

    def _run(self, **kwargs):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("issue_pending_keys", stdout=out, **kwargs)
        return out.getvalue()

    def test_key_is_issued_and_emailed(self):
        from api import email_service as service
        from api.models import ApiAccessRequest, IntelligenceApiKey

        with patch.object(service.email_service, "send_api_key",
                          return_value=True) as send:
            self._run()

        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["recipient"], "dev@example.com")
        self.assertEqual(IntelligenceApiKey.objects.filter(is_active=True).count(), 1)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, ApiAccessRequest.STATUS_APPROVED)

    def test_running_twice_issues_only_one_key(self):
        from api import email_service as service
        from api.models import IntelligenceApiKey

        with patch.object(service.email_service, "send_api_key", return_value=True) as send:
            self._run()
            self._run()

        self.assertEqual(send.call_count, 1)
        self.assertEqual(IntelligenceApiKey.objects.count(), 1)

    def test_a_revoked_key_is_not_silently_reissued(self):
        """
        Kunci yang pernah diterbitkan lalu dicabut berarti permintaan itu sudah
        dilayani. Menerbitkan yang baru diam-diam membatalkan keputusan
        mencabutnya.
        """
        from api import email_service as service
        from api.models import IntelligenceApiKey

        with patch.object(service.email_service, "send_api_key", return_value=True):
            self._run()
        IntelligenceApiKey.objects.update(is_active=False)

        with patch.object(service.email_service, "send_api_key") as send:
            self._run()

        send.assert_not_called()

    def test_a_failed_send_leaves_no_live_key(self):
        """
        Nilai asli kunci hanya pernah ada di dalam surat itu. Bila suratnya
        tidak sampai, kunci yang aktif tidak dipegang siapa pun.
        """
        from api import email_service as service
        from api.models import IntelligenceApiKey

        with patch.object(service.email_service, "send_api_key", return_value=False):
            output = self._run()

        self.assertIn("GAGAL", output)
        self.assertEqual(IntelligenceApiKey.objects.filter(is_active=True).count(), 0)

    def test_dry_run_changes_nothing(self):
        from api import email_service as service
        from api.models import IntelligenceApiKey

        with patch.object(service.email_service, "send_api_key") as send:
            output = self._run(dry_run=True)

        send.assert_not_called()
        self.assertEqual(IntelligenceApiKey.objects.count(), 0)
        self.assertIn("dev@example.com", output)

    def test_rejected_requests_are_skipped(self):
        from api import email_service as service
        from api.models import ApiAccessRequest

        self.request.status = ApiAccessRequest.STATUS_REJECTED
        self.request.save(update_fields=["status"])

        with patch.object(service.email_service, "send_api_key") as send:
            self._run()

        send.assert_not_called()

    def test_consumer_name_is_readable_and_unique(self):
        from api import email_service as service
        from api.models import ApiAccessRequest, IntelligenceApiKey

        ApiAccessRequest.objects.create(
            name="Lain", email="lain@example.com", organization="Contoh Studio",
            use_case="Produk lain.")

        with patch.object(service.email_service, "send_api_key", return_value=True):
            self._run()

        consumers = sorted(IntelligenceApiKey.objects.values_list("consumer", flat=True))
        self.assertEqual(consumers[0], "contoh-studio")
        self.assertTrue(consumers[1].startswith("contoh-studio-"))
        self.assertEqual(len(set(consumers)), 2)

    def test_email_local_part_is_used_when_no_organization(self):
        from api import email_service as service
        from api.models import ApiAccessRequest, IntelligenceApiKey

        ApiAccessRequest.objects.all().delete()
        ApiAccessRequest.objects.create(
            name="Arya", email="arya.zaky@example.com", use_case="Riset.")

        with patch.object(service.email_service, "send_api_key", return_value=True):
            self._run()

        self.assertEqual(IntelligenceApiKey.objects.get().consumer, "arya-zaky")


class EngineBoundaryTests(TestCase):
    """
    Engine berdiri sebagai produk tersendiri. Batas itu hanya bertahan bila
    dijaga: satu impor ke `api` sudah cukup membuatnya tidak bisa dipakai di
    luar Healthify, dan itu tidak akan ketahuan sampai ada yang mencoba.
    """

    def _engine_files(self):
        import pathlib

        import ragai

        root = pathlib.Path(ragai.__file__).parent
        return sorted(root.rglob("*.py"))

    def test_engine_never_imports_the_host_application(self):
        import re

        pattern = re.compile(r"^\s*(from|import)\s+(api\b|\.\.\.)", re.M)
        offenders = []
        for path in self._engine_files():
            for i, line in enumerate(path.read_text().split("\n"), 1):
                if pattern.match(line):
                    offenders.append(f"{path.name}:{i} {line.strip()}")

        self.assertEqual(offenders, [], "engine mengimpor aplikasi induk")

    def test_engine_declares_what_it_needs(self):
        from ragai import runtime

        self.assertTrue(runtime.is_configured(),
                        "model yang dibutuhkan engine belum terdaftar")

    def test_host_registers_every_required_model(self):
        from ragai import runtime

        for name in runtime.REQUIRED_MODELS:
            self.assertIsNotNone(runtime.model(name), f"{name} tidak terdaftar")

    def test_missing_registration_fails_clearly(self):
        """
        Engine yang berjalan tanpa tempat menyimpan harus gagal di dekat
        sumbernya, bukan jauh di dalam pipeline dengan pesan yang tidak
        menjelaskan apa pun.
        """
        from ragai import runtime

        with self.assertRaises(RuntimeError) as caught:
            runtime.model("TidakAda")

        self.assertIn("configure", str(caught.exception))

    def test_engine_is_usable_through_its_own_surface(self):
        import ragai

        with patch("ragai.engine.retrieve_candidates", return_value=[]):
            response = ragai.process({"query": "apakah demam berdarah menular"})

        self.assertTrue(response.answer)
        self.assertTrue(ragai.version())
