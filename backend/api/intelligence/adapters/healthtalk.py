"""
Adapter response untuk consumer eksternal (§13, §21).

    Internal Response
           |
     +-----+------+
     v            v
  Existing     HealthTalk
  API Format   API Format

HealthTalk hanya melihat bentuk di bawah ini; ia tidak perlu tahu apa pun
tentang implementasi internal Healthify.
"""

import re
from typing import Any, Dict

from ..contracts import IntelligenceResponse

# Penanda sitasi internal ("[E1]") berguna untuk penelusuran di bentuk `full`,
# tetapi hanya menjadi derau pada teks yang langsung ditampilkan ke pengguna.
_CITATION_MARKER_RE = re.compile(r"\s*\[E\d{1,2}\]")


def to_consumer_response(response: IntelligenceResponse,
                         include_evidence: bool = True,
                         include_sources: bool = True) -> Dict[str, Any]:
    """Bentuk response publik untuk endpoint /api/v1/intelligence/query."""
    data: Dict[str, Any] = {
        "answer": response.answer,
        "intent": response.intent.value,
        "mode": response.mode.value,
        "conversation_id": response.conversation_id,
        "health_context": response.health_context.to_dict(),
        "evidence": (
            [item.to_public_dict() for item in response.evidence]
            if include_evidence else []
        ),
        "claims": [claim.to_dict() for claim in response.claims],
        "evidence_status": response.evidence_status.value,
        "uncertainty": response.uncertainty,
        "safety": {
            "decision": response.safety_decision.value,
            "flags": [flag.to_dict() for flag in response.safety_flags],
        },
        "safety_flags": [flag.to_dict() for flag in response.safety_flags],
        "preliminary_assessment": response.preliminary_assessment,
        "metadata": response.metadata,
    }

    if not include_sources:
        for item in data["evidence"]:
            item.pop("doi", None)
            item.pop("url", None)

    return data


def to_simple_response(response: IntelligenceResponse) -> Dict[str, Any]:
    """
    Bentuk ringkas untuk consumer yang hanya butuh **informasi kesehatan
    beserta sumber jurnalnya**.

    Sengaja TANPA label apa pun (tidak ada valid/hoax/uncertain, tidak ada
    intent, tidak ada status internal): label verifikasi adalah urusan produk
    Healthify, bukan sesuatu yang harus ditafsirkan consumer.

    Field:
        answer          teks jawaban siap tampil
        sources         jurnal pendukung; `url` selalu tautan yang sudah
                        dipastikan hidup, atau null bila tidak ada
        has_evidence    False berarti tidak ada bukti memadai dan `answer`
                        berisi pernyataan jujur soal itu — jangan diproses
                        seolah jawaban biasa
        notice          teks peringatan yang WAJIB ditampilkan bila terisi
                        (mis. keluhan menandakan kondisi gawat darurat)
        conversation_id kirim balik pada permintaan berikutnya agar konteks
                        percakapan tersambung
        request_id      untuk pelaporan masalah
    """
    critical = [
        flag for flag in response.safety_flags
        if getattr(flag, "severity", "") == "critical"
    ]
    notice = critical[0].message if critical else None

    answer = _CITATION_MARKER_RE.sub("", response.answer or "")
    answer = re.sub(r"\s+([.,;])", r"\1", answer)
    answer = re.sub(r"[ \t]{2,}", " ", answer).strip()

    return {
        "answer": answer,
        "sources": [
            {
                "title": item.title,
                "url": item.url or None,
                "doi": item.doi or None,
                "publisher": item.publisher or None,
                "year": item.published_year,
                "relevance": round(float(item.relevance), 3),
                "snippet": (item.snippet or "")[:400],
            }
            for item in response.evidence
        ],
        "has_evidence": bool(response.evidence),
        "notice": notice,
        "conversation_id": response.conversation_id,
        "request_id": (response.metadata or {}).get("request_id"),
    }
