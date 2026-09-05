"""
Spesifikasi OpenAPI 3.1 untuk API publik Healthify.

Dibangun sebagai dict Python (bukan file statis) supaya beberapa nilai —
versi engine, mode autentikasi, daftar intent — selalu sinkron dengan
konfigurasi yang benar-benar berjalan.

Dirender oleh Scalar di `/docs`.
"""

from typing import Any, Dict


def _contact_block() -> Dict[str, Any]:
    """
    Blok kontak untuk `info`.

    `url` dan `email` hanya disertakan bila benar-benar dikonfigurasi. Field
    bertipe URL yang diisi teks biasa akan dirender Scalar sebagai tautan dan
    menghasilkan tautan rusak.
    """
    from django.conf import settings

    contact: Dict[str, Any] = {
        "name": getattr(settings, "ENGINE_BRAND_NAME", "ragai"),
    }
    url = getattr(settings, "API_CONTACT_URL", "")
    email = getattr(settings, "API_CONTACT_EMAIL", "")
    if url.startswith(("http://", "https://")):
        contact["url"] = url
    if "@" in email:
        contact["email"] = email
    return contact


def _access_request_note() -> str:
    """Baris penutup yang menyebut ke mana permintaan akses dikirim."""
    from django.conf import settings

    url = getattr(settings, "API_CONTACT_URL", "")
    email = getattr(settings, "API_CONTACT_EMAIL", "")

    if url.startswith(("http://", "https://")):
        target = f"[{url}]({url})"
    elif "@" in email:
        target = email
    else:
        return ""

    return (
        "\n\n## Contact\n\n"
        f"Send access requests and integration questions to {target}. "
        "Include the `request_id` from any response you are reporting."
    )


def _enum_values(enum_cls):
    return [member.value for member in enum_cls]


def build_openapi_spec(base_url: str = "") -> Dict[str, Any]:
    from django.conf import settings

    from ragai.contracts import (
        EvidenceStatus,
        Intent,
        Mode,
        Provenance,
        SafetyDecision,
    )
    from ragai.engine import ENGINE_VERSION

    auth_required = bool(getattr(settings, "INTELLIGENCE_API_KEYS", None))
    ALLOWED_HOSTS = [
        h for h in (getattr(settings, "ALLOWED_HOSTS", []) or [])
        if h and not h.startswith(".") and h not in ("localhost", "127.0.0.1", "*", "testserver")
    ]

    # A single canonical production server. Enumerating every configured host
    # would put internal deployment detail into a public contract.
    public_hosts = [
        host for host in (getattr(settings, "ALLOWED_HOSTS", []) or [])
        if host and not host.startswith(".")
        and host not in ("localhost", "127.0.0.1", "*", "testserver")
    ]
    # Alamat publik engine berasal dari konfigurasi, bukan ditebak dari daftar
    # host yang diizinkan: daftar itu memuat domain produk Healthify juga, dan
    # menebak dari sana pernah membuat dokumentasi menunjuk domain yang salah.
    configured = getattr(settings, "PUBLIC_API_BASE_URL", "")
    if configured.startswith(("http://", "https://")):
        server_url = configured.rstrip("/")
    else:
        server_url = f"https://{public_hosts[0]}" if public_hosts else (base_url or "/")
    servers = [{"url": server_url, "description": "Production"}]

    spec: Dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "ragai Health Intelligence API",
            "version": ENGINE_VERSION,
            "summary": "Health information grounded in peer reviewed journal literature.",
            "description": _DESCRIPTION.strip() + _access_request_note(),
            "contact": _contact_block(),
            "license": {"name": "Proprietary"},
        },
        "servers": servers,
        # Default: endpoint memerlukan API key. Endpoint publik Healthify
        # menimpanya dengan `"security": []` di masing-masing operation.
        "security": [{"ApiKeyAuth": []}],
        "tags": [
            {
                "name": "Health Intelligence",
                "description": (
                    "Send a health question, receive an answer with the journal "
                    "sources behind it. The full integration guide is at the top "
                    "of this page."
                ),
            },
            {
                "name": "Conversations",
                "description": (
                    "Carries context between turns and produces a structured "
                    "summary at the end of a session. Optional: single questions "
                    "do not need it."
                ),
            },
        ],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": (
                        "Your API key, sent on every request. Request access to "
                        "receive one."
                        + ("" if auth_required else
                           " Not yet configured on this deployment, so endpoints "
                           "are currently open.")
                    ),
                },
            },
            "schemas": _schemas(
                intents=_enum_values(Intent),
                modes=_enum_values(Mode),
                evidence_status=_enum_values(EvidenceStatus),
                safety_decisions=_enum_values(SafetyDecision),
                provenance=_enum_values(Provenance),
            ),
        },
        "paths": _paths(),
    }
    return spec


_DESCRIPTION = r"""
Send one health question. Receive an answer grounded in peer reviewed journal
literature, together with the sources it was built from. Every DOI is checked
against the official registry before it is returned, so the links you receive
resolve, and every title is taken from the registry rather than from the model.

When the literature does not cover a question, the engine says so rather than
writing something plausible. Treat that as the useful case it is: it is the
difference between a citation you can hand to a reader and one you cannot.

There is no SDK and no OAuth. One POST request, one header.

**Status: beta.** Access is free while the engine is in beta. The index is still
growing, answers can be shallow on topics with thin literature, and the response
may gain fields. Fields are added, not removed or renamed, and key holders are
notified by email before anything breaking ships.

## Requesting access

This API is available on request. Use the **Request API access** button at the
bottom right of this page, describe your product and expected volume, and you
will receive an API key in the form `ht_live_xxxxxxxxxxxxxxxxxxxx` at the email
address you provide.

Store the key as an environment variable on your server. Never place it in
frontend code, where anyone can read it from the browser.

A single consumer may hold several keys at once, for example one per
environment or per application, so that one key can be replaced or revoked
without interrupting the others. Ask for as many as your deployment needs.

Rate limits are set per key. If your backend serves many users at once, say so
in your request and a higher limit can be issued for your key alone.

## Quickstart

### 1. Send a question

```bash
curl -X POST https://ragai.twenti.studio/api/v1/intelligence/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "query": "What causes dengue fever?",
    "mode": "information",
    "options": { "format": "simple", "max_evidence": 3 }
  }'
```

```javascript
async function askHealthify(question) {
  const res = await fetch(
    "https://ragai.twenti.studio/api/v1/intelligence/query",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": process.env.HEALTHIFY_API_KEY,
      },
      body: JSON.stringify({
        query: question,
        mode: "information",
        options: { format: "simple", max_evidence: 3 },
      }),
    }
  );

  if (!res.ok) throw new Error(`Healthify ${res.status}`);
  return res.json();
}
```

### 2. Read the response

With `format: "simple"` the response has six fields:

```json
{
  "answer": "Dengue fever is caused by the dengue virus, which is transmitted by mosquitoes. It can affect anyone, and in some people it becomes more severe.",
  "sources": [
    {
      "title": "Dengue Fever: An Overview",
      "url": "https://doi.org/10.5772/intechopen.92315",
      "doi": "10.5772/intechopen.92315",
      "publisher": "IntechOpen",
      "year": 2020,
      "relevance": 0.553,
      "snippet": "Dengue fever is a disease caused by a family of viruses transmitted by mosquitoes..."
    }
  ],
  "has_evidence": true,
  "notice": null,
  "conversation_id": null,
  "request_id": "9f2c1a..."
}
```

Show `answer` and `sources` to your users. There are no assessment labels to
interpret or translate.

## Request body

Only `query` is required.

| Field | Required | Description |
|-|-|-|
| `query` | Yes | The user question, unmodified. Up to 5,000 characters. |
| `mode` | No | `information` for knowledge questions, `consultation` when the user describes symptoms, `medication` for drug questions, `claim` to assess whether a health claim holds. Defaults to `consultation`. |
| `options.format` | No | `"simple"` returns the answer and its sources only. Use this. The default `"full"` returns the complete internal structure. |
| `options.max_evidence` | No | Number of journals returned, 1 to 20. Defaults to 8. |
| `context.conversation_id` | No | Your own chat room identifier. `session_id`, `room_id`, `thread_id` and `chat_id` are accepted too. See Multi turn conversations below. |

Send the question as the user wrote it. Do not prepend your own instructions or
prompt text. The engine already handles question understanding, literature
retrieval, and answer composition; added prompt text degrades retrieval quality.

## Response fields

| Field | Type | Description |
|-|-|-|
| `answer` | string | Display ready text. Free of internal jargon and citation markers. |
| `sources` | array | Supporting journals, ordered by relevance. |
| `has_evidence` | boolean | `false` means no adequate evidence was found. |
| `notice` | string or null | When set, something urgent applies and must be shown prominently. |
| `conversation_id` | string or null | Present when you supply a `session_id`. |
| `request_id` | string | Record this. Include it when reporting a problem. |

Each entry in `sources`:

| Field | Description |
|-|-|
| `title` | Journal title in its original language. |
| `url` | A link that has been confirmed to resolve. May be `null`, see below. |
| `doi` | Normalised and verified DOI. |
| `publisher`, `year` | For rendering the citation. |
| `relevance` | 0 to 1. Already sorted descending; no need to sort again. |
| `snippet` | Abstract excerpt, up to 400 characters. |

A `null` value in `url` is deliberate. It means the system could not confirm the
link resolves, and it prefers returning no link over a dead one. Do not build
`https://doi.org/{doi}` yourself; doing so reintroduces the broken links this
behaviour exists to prevent.

## Three things you must handle

**Honour `notice`.** When it is set, the user described something that needs
prompt medical attention. The `answer` already opens with a warning, but display
the notice prominently rather than burying it in a chat bubble.

**Treat `has_evidence: false` differently.** It means no relevant literature was
found and the system deliberately did not guess. Show the `answer`, but do not
present it as sourced, and do not render an empty reference list.

**Always show the sources.** Traceability is the point of this service. An
answer without its sources removes the only way a user can verify it.

```javascript
const result = await askHealthify(userQuestion);

if (result.notice) {
  showUrgentWarning(result.notice);   // prominent, above the answer
}

showAnswer(result.answer);

if (result.has_evidence) {
  showReferences(result.sources);     // title, link, year
} else {
  markAsUnsourced();                  // do not render an empty list
}
```

## Multi turn conversations

### What to send

Send an identifier for the chat room on every turn. Use whatever identifier
your product already has: a room id, a thread id, a ticket number. Nothing has
to be registered in advance, and there is no separate call to open a session.
The engine creates it on first use and recognises it from then on.

Any one of these field names is accepted, so you can send the field you already
have:

```json
{
  "query": "Is that normal?",
  "context": { "conversation_id": "room-8812" }
}
```

| Accepted field | Note |
|-|-|
| `context.conversation_id` | Preferred |
| `context.session_id` | Equivalent |
| `context.room_id` | Equivalent |
| `context.thread_id` | Equivalent |
| `context.chat_id` | Equivalent |

Identifiers are scoped to your API key. Two products may both use `"room-1"`
without ever seeing each other's conversations.

You do not resend previous messages. The engine keeps the history and the
health context it has accumulated, and resolves references like "that" or
"it" against earlier turns.

```javascript
const room = { conversation_id: "room-8812" };

await ask({ query: "I have a fever",  mode: "consultation", context: room });
await ask({ query: "For three days",  mode: "consultation", context: room });
await ask({ query: "Is that normal?", mode: "consultation", context: room });
// the third turn understands "that" as a three day fever
```

If your backend prefers to keep the history itself, omit the identifier and
send `context.previous_messages` instead. The engine then holds no state.

### Which journals answer a follow up

Inside one room a topic is discussed across several messages, and the answers
have to stay consistent with each other. The engine follows one rule:

1. The first question searches the literature and selects the journals that
   answer it.
2. A follow up is checked against those journals. While they still answer the
   question, the same journals are used again, so the answers in that room
   stay anchored to the same references rather than shifting between turns.
3. When the question moves beyond what those journals cover, a fresh search
   runs and new references are selected.

Every response tells you which of the two happened, so you can decide how to
render the references:

| Field | Meaning |
|-|-|
| `sources_reused: true` | same references as the previous turn in this room |
| `sources_reused: false` | references were selected by a new search |

A common use is to print the reference list once when it changes, and to omit
it on turns that reuse it, instead of repeating the same citations under every
message.

### Closing a conversation

At the end of a session, `POST /api/v1/intelligence/summary` with
`{"session_id": "room-8812"}` returns a structured summary in which every part
carries the provenance of its information.

## Backend integration

This section matters when the engine is called from another application's
backend rather than from a user's browser.

### Latency: keep it off the synchronous request path

A request takes 2 to 10 seconds because it performs literature retrieval,
ranking, DOI verification, and a language model call. Do not hold your user's
HTTP request open for that long. Safe patterns:

* run it in a background job or queue, then deliver the result over websocket,
  polling, or push;
* or show a "searching the literature" state and update when the result arrives.

Set a client timeout of at least 30 seconds. A short timeout triggers retries,
which multiply cost.

### Idempotency: required if you retry

Because requests are slow, retrying after a timeout is normal. Without
protection, a retry duplicates the conversation turn and pays the language model
provider twice.

Send an `X-Idempotency-Key` header containing an identifier of your own, such as
a job id:

```bash
-H "X-Idempotency-Key: job-8f21c4"
```

A repeat request with the same key returns the original response unchanged,
without reprocessing, and carries the header `X-Idempotent-Replay: true`. Keys
are valid for 24 hours and are scoped per API key.

### Log correlation

Send `X-Request-Id` containing your own trace id. The value is used as given,
returned in the response header, and appears in the `request_id` field,
including on error responses. When omitted, Healthify generates one.

```
X-Request-Id: 8f21c4de-trace
```

Accepted format is 8 to 64 characters from `A-Z a-z 0-9 . _ : and the hyphen`.
Values outside that are replaced, so untrusted input never reaches the logs.

### Rate limits

The default is 60 requests per minute per API key. Limits are independent per
consumer, so one integration cannot exhaust another's allowance. Handle `429` by
respecting the `Retry-After` header rather than retrying immediately.

### Keeping your own history

If your backend already stores the conversation, you do not need `session_id`.
Send `context.previous_messages` as an array of `{role, content}` and the engine
uses it without storing anything:

```json
{
  "query": "Is that normal?",
  "mode": "consultation",
  "context": {
    "previous_messages": [
      { "role": "user",      "content": "I have a fever" },
      { "role": "assistant", "content": "How long has it lasted?" },
      { "role": "user",      "content": "Three days" }
    ]
  }
}
```

## Errors

Every error returns JSON containing `error` and `detail`.

| Status | Meaning | What to do |
|-|-|-|
| 400 | `invalid_request` | `query` is empty, too long, or `mode` is unrecognised. Fix the request; retrying will not help. |
| 401 | `unauthorized` | The `X-API-Key` header is missing or unknown. |
| 404 | `not_found` | Session endpoints only: the `session_id` has never been used. |
| 429 | Rate limit exceeded | Wait for the number of seconds in `Retry-After`, then retry. |
| 500 | `engine_error` | Retry once. If it persists, report it with the `request_id`. |

```javascript
async function askWithRetry(question, attemptsLeft = 2) {
  const res = await send(question);

  if (res.status === 429 && attemptsLeft > 0) {
    const wait = Number(res.headers.get("Retry-After") ?? 5);
    await new Promise((r) => setTimeout(r, wait * 1000));
    return askWithRetry(question, attemptsLeft - 1);
  }

  return res.json();
}
```

## Limits

| Item | Value |
|-|-|
| Rate limit | 60 requests per minute, per API key |
| Question length | up to 5,000 characters |
| Sources per request | 1 to 10 |
| Response time | typically 2 to 10 seconds |
| Answer language | follows the language of the question |

Keep your API key on your server, never in browser code. Store it as an
environment variable rather than in a repository. If a key is exposed, request a
replacement; the old key can be revoked without interrupting service.

This service returns health information drawn from published literature. It is
not a medical diagnosis. State that clearly in your interface, and do not
present answers as a clinical assessment of the user's condition.

## Source integrity

Every source comes from the Healthify knowledge base, never from language model
output.

* DOIs are verified against the DOI Handle System and Crossref before
  publication.
* DOIs that are unregistered or malformed are discarded.
* When a link's status cannot be confirmed, `url` is left empty.
* When evidence is inadequate, `has_evidence` is `false` and the system does not
  guess.
"""


def _schemas(intents, modes, evidence_status, safety_decisions, provenance):
    return {
        "Error": {
            "type": "object",
            "properties": {
                "error": {"type": "string", "examples": ["invalid_request"]},
                "detail": {"type": "string"},
            },
        },
        "HealthContext": {
            "type": "object",
            "description": (
                "Structured health context extracted from the conversation. Fields "
                "the user did not report stay `null` or empty; the system never "
                "invents their contents."
            ),
            "properties": {
                "chief_complaint": {"type": ["string", "null"], "examples": ["demam"]},
                "symptoms": {"type": "array", "items": {"type": "string"},
                             "examples": [["demam", "batuk"]]},
                "duration": {"type": ["string", "null"], "examples": ["3 hari"]},
                "severity": {"type": ["string", "null"], "examples": ["sedang"]},
                "onset": {"type": ["string", "null"], "examples": ["mendadak"]},
                "progression": {"type": ["string", "null"], "examples": ["memburuk"]},
                "associated_symptoms": {"type": "array", "items": {"type": "string"}},
                "medications": {"type": "array", "items": {"type": "string"}},
                "allergies": {"type": "array", "items": {"type": "string"}},
                "relevant_history": {"type": "array", "items": {"type": "string"}},
                "provenance": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "enum": provenance},
                    "description": "Asal-usul tiap field.",
                },
            },
        },
        "Evidence": {
            "type": "object",
            "description": "One piece of evidence from the Healthify knowledge base.",
            "properties": {
                "source_id": {"type": "string", "examples": ["journal:12"]},
                "chunk_id": {"type": "string"},
                "title": {"type": "string"},
                "doi": {
                    "type": ["string", "null"],
                    "description": "Set only when the DOI passes format validation.",
                    "examples": ["10.1016/j.jclinepi.2020.03.001"],
                },
                "url": {
                    "type": ["string", "null"],
                    "description": (
                        "Set only when the link has been confirmed reachable. `null` "
                        "means the system cannot guarantee the link resolves."
                    ),
                },
                "publisher": {"type": ["string", "null"]},
                "published_year": {"type": ["integer", "null"]},
                "source_type": {"type": "string", "examples": ["journal"]},
                "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                "snippet": {"type": "string"},
                "origin": {
                    "type": "string",
                    "enum": ["KNOWLEDGE_BASE", "VECTOR_INDEX", "VERIFIED_REGISTRY",
                             "USER_SUPPLIED", "MODEL_SUGGESTED"],
                    "description": (
                        "Where the evidence came from. `MODEL_SUGGESTED` never appears "
                        "in a response: model invented sources are discarded before "
                        "publication."
                    ),
                },
                "doi_verified": {"type": "boolean"},
                "link_status": {
                    "type": "string",
                    "enum": ["verified", "unknown", "unresolvable", "malformed", "skipped", "unchecked"],
                },
            },
        },
        "SupportedClaim": {
            "type": "object",
            "description": "Traces statements in the answer back to their supporting evidence.",
            "properties": {
                "claim": {"type": "string"},
                "verdict": {"type": "string", "enum": ["supported", "unsupported",
                                                       "inconclusive"]},
                "confidence": {"type": ["number", "null"]},
                "supporting_evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_id": {"type": "string"},
                            "source_id": {"type": "string"},
                            "title": {"type": "string"},
                            "doi": {"type": ["string", "null"]},
                            "url": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
        "SafetyFlag": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "enum": ["EMERGENCY_SIGNAL", "DANGEROUS_INSTRUCTION",
                             "TREATMENT_RECOMMENDATION_RISK", "DIAGNOSIS_CERTAINTY",
                             "OVERCONFIDENT_CLAIM", "UNSUPPORTED_MEDICAL_CLAIM",
                             "INSUFFICIENT_EVIDENCE", "HIGH_RISK_POPULATION",
                             "MEDICATION_CONTEXT"],
                },
                "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                "message": {"type": "string"},
            },
        },
        "QueryRequest": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string", "maxLength": 5000,
                    "examples": ["I have had a fever for three days."],
                },
                "mode": {
                    "type": "string", "enum": modes, "default": "consultation",
                    "description": (
                        "`information` for knowledge questions, `consultation` when "
                        "the user describes symptoms, `medication` for drug questions, "
                        "`claim` to assess whether a health claim holds."
                    ),
                },
                "context": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {"type": "string",
                                            "description": "Alias for `session_id`."},
                        "session_id": {"type": "string", "examples": ["HT-001"]},
                        "health_context": {"$ref": "#/components/schemas/HealthContext"},
                        "previous_messages": {
                            "type": "array",
                            "description": (
                                "Conversation history for stateless use. When "
                                "`session_id` is supplied, Healthify stores the history "
                                "and this field is optional."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string", "enum": ["user", "assistant"]},
                                    "content": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "options": {
                    "type": "object",
                    "properties": {
                        "include_evidence": {"type": "boolean", "default": True},
                        "include_sources": {"type": "boolean", "default": True},
                        "format": {
                            "type": "string", "enum": ["simple", "full"], "default": "full",
                            "description": (
                                "`simple` returns only the answer and its journal "
                                "sources, with no labels, intent, or internal metadata. "
                                "`full` returns the complete structure."
                            ),
                        },
                        "max_evidence": {"type": "integer", "minimum": 1, "maximum": 20,
                                         "default": 8},
                        "language": {"type": "string", "enum": ["id", "en"], "default": "id"},
                    },
                },
            },
        },
        "SimpleQueryResponse": {
            "type": "object",
            "description": (
                "The compact shape (`options.format: \"simple\"`): health information "
                "with its journal sources. Contains no assessment labels."
            ),
            "properties": {
                "answer": {"type": "string", "description": "Display ready answer text."},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": ["string", "null"],
                                    "description": "A link confirmed to resolve, or "
                                                   "null when none is available."},
                            "doi": {"type": ["string", "null"]},
                            "publisher": {"type": ["string", "null"]},
                            "year": {"type": ["integer", "null"]},
                            "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                            "snippet": {"type": "string"},
                        },
                    },
                },
                "has_evidence": {
                    "type": "boolean",
                    "description": "False means no adequate evidence was found; the "
                                   "`answer` states this plainly.",
                },
                "notice": {
                    "type": ["string", "null"],
                    "description": "When set, this must be shown to the user, for "
                                   "example an emergency signal.",
                },
                "conversation_id": {"type": ["string", "null"]},
                "sources_reused": {
                    "type": "boolean",
                    "description": "True means this turn is anchored to the same "
                                   "journals as the previous turn in this room, "
                                   "rather than to a fresh search. Useful for "
                                   "printing the reference list only when it "
                                   "changes.",
                },
                "request_id": {"type": ["string", "null"]},
            },
        },
        "FullQueryResponse": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "intent": {"type": "string", "enum": intents},
                "mode": {"type": "string", "enum": modes},
                "conversation_id": {"type": ["string", "null"]},
                "health_context": {"$ref": "#/components/schemas/HealthContext"},
                "evidence": {"type": "array",
                             "items": {"$ref": "#/components/schemas/Evidence"}},
                "claims": {"type": "array",
                           "items": {"$ref": "#/components/schemas/SupportedClaim"}},
                "evidence_status": {"type": "string", "enum": evidence_status},
                "uncertainty": {"type": ["string", "null"]},
                "safety": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": safety_decisions},
                        "flags": {"type": "array",
                                  "items": {"$ref": "#/components/schemas/SafetyFlag"}},
                    },
                },
                "safety_flags": {"type": "array",
                                 "items": {"$ref": "#/components/schemas/SafetyFlag"}},
                "preliminary_assessment": {
                    "type": ["object", "null"],
                    "description": "Asesmen awal — **bukan diagnosis**.",
                    "properties": {
                        "status": {"type": "string", "const": "PRELIMINARY_ASSESSMENT"},
                        "is_diagnosis": {"type": "boolean", "const": False},
                        "disclaimer": {"type": "string"},
                        "reported_symptoms": {"type": "array", "items": {"type": "string"}},
                        "duration": {"type": ["string", "null"]},
                        "urgency": {"type": "string",
                                    "enum": ["routine", "elevated", "emergency"]},
                        "evidence_confidence": {"type": "string",
                                                "enum": ["very_low", "low", "moderate"]},
                        "recommended_next_step": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "metadata": {"type": "object"},
            },
        },
        "ConsultationSummary": {
            "type": "object",
            "description": (
                "Ringkasan konsultasi. Setiap bagian membawa `provenance`: "
                "`USER_REPORTED`, `AI_INFERRED`, `EVIDENCE_SUPPORTED`, or "
                "`SYSTEM_GENERATED`."
            ),
            "properties": {
                "session_id": {"type": "string"},
                "status": {"type": "string", "const": "PRELIMINARY_ASSESSMENT"},
                "is_diagnosis": {"type": "boolean", "const": False},
                "chief_complaint": {"$ref": "#/components/schemas/ProvenanceValue"},
                "symptoms": {"type": "array",
                             "items": {"$ref": "#/components/schemas/ProvenanceValue"}},
                "duration": {"$ref": "#/components/schemas/ProvenanceValue"},
                "relevant_information": {"type": "array",
                                         "items": {"$ref": "#/components/schemas/ProvenanceValue"}},
                "preliminary_assessment": {"$ref": "#/components/schemas/ProvenanceValue"},
                "evidence_discussed": {"type": "array", "items": {"type": "object"}},
                "recommended_next_step": {"type": "array",
                                          "items": {"$ref": "#/components/schemas/ProvenanceValue"}},
                "safety_notes": {"type": "array",
                                 "items": {"$ref": "#/components/schemas/ProvenanceValue"}},
                "health_context": {"$ref": "#/components/schemas/HealthContext"},
            },
        },
        "ProvenanceValue": {
            "type": ["object", "null"],
            "properties": {
                "value": {},
                "provenance": {"type": "string", "enum": provenance},
                "detail": {"type": ["string", "null"]},
            },
        },
    }


def _paths():
    """
    Only the endpoints intended for external consumers.

    Healthify's internal product endpoints (labelled claim verification, claim
    history, disputes, translation, health check, and everything under
    /api/admin/*) are deliberately absent: this document is the contract for
    parties using the engine, not a map of the whole application.
    """
    return {
        "/api/v1/intelligence/query": {
            "post": {
                "tags": ["Health Intelligence"],
                "summary": "Ask a health question",
                "x-codeSamples": [
                    {
                        "lang": "cURL",
                        "label": "curl",
                        "source": (
                            "curl -X POST https://ragai.twenti.studio/api/v1/intelligence/query \\\n"
                            "  -H \"Content-Type: application/json\" \\\n"
                            "  -H \"X-API-Key: API_KEY\" \\\n"
                            "  -d '{\n"
                            "    \"query\": \"What causes dengue fever?\",\n"
                            "    \"mode\": \"information\",\n"
                            "    \"options\": { \"format\": \"simple\", \"max_evidence\": 3 }\n"
                            "  }'"
                        ),
                    },
                    {
                        "lang": "JavaScript",
                        "label": "fetch (server-side)",
                        "source": (
                            "const res = await fetch(\n"
                            "  \"https://ragai.twenti.studio/api/v1/intelligence/query\",\n"
                            "  {\n"
                            "    method: \"POST\",\n"
                            "    headers: {\n"
                            "      \"Content-Type\": \"application/json\",\n"
                            "      \"X-API-Key\": process.env.HEALTHIFY_API_KEY,\n"
                            "    },\n"
                            "    body: JSON.stringify({\n"
                            "      query: \"What causes dengue fever?\",\n"
                            "      mode: \"information\",\n"
                            "      options: { format: \"simple\", max_evidence: 3 },\n"
                            "    }),\n"
                            "  }\n"
                            ");\n\n"
                            "if (!res.ok) throw new Error(`Healthify ${res.status}`);\n"
                            "const { answer, sources, has_evidence, notice } = await res.json();"
                        ),
                    },
                    {
                        "lang": "Python",
                        "label": "requests",
                        "source": (
                            "import os, requests\n\n"
                            "res = requests.post(\n"
                            "    \"https://ragai.twenti.studio/api/v1/intelligence/query\",\n"
                            "    headers={\"X-API-Key\": os.environ[\"HEALTHIFY_API_KEY\"]},\n"
                            "    json={\n"
                            "        \"query\": \"What causes dengue fever?\",\n"
                            "        \"mode\": \"information\",\n"
                            "        \"options\": {\"format\": \"simple\", \"max_evidence\": 3},\n"
                            "    },\n"
                            "    timeout=30,\n"
                            ")\n"
                            "res.raise_for_status()\n"
                            "data = res.json()\n"
                            "print(data[\"answer\"])\n"
                            "for s in data[\"sources\"]:\n"
                            "    print(s[\"title\"], s[\"url\"])"
                        ),
                    },
                ],
                "description": (
                    "The primary endpoint. Internally:\n\n"
                    "```\n"
                    "Question -> Query understanding -> Health context\n"
                    "         -> Literature retrieval -> Source validation\n"
                    "         -> Answer composition -> Safety layer -> Response\n"
                    "```\n\n"
                    "Use `options.format: \"simple\"` unless you genuinely need the "
                    "complete internal structure.\n\n"
                    "**Multi turn:** send the same `context.session_id` on every turn. "
                    "Healthify retains the history and accumulated context, so "
                    "\"I have a fever\" followed by \"For three days\" is understood as "
                    "one complaint lasting three days.\n\n"
                    "**When evidence is inadequate,** `has_evidence` is `false` and the "
                    "answer says so plainly. The model is never asked to guess."
                ),
                "parameters": [
                    {
                        "name": "X-Idempotency-Key", "in": "header", "required": False,
                        "schema": {"type": "string", "maxLength": 128},
                        "description": (
                            "An identifier of your own. A repeat request with the same "
                            "key returns the original response without reprocessing "
                            "(header `X-Idempotent-Replay: true`). Valid for 24 hours. "
                            "**Required if you retry.**"
                        ),
                        "example": "job-8f21c4",
                    },
                    {
                        "name": "X-Request-Id", "in": "header", "required": False,
                        "schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,64}$"},
                        "description": (
                            "Your own trace id for log correlation. Returned in the "
                            "response header and in the `request_id` field."
                        ),
                        "example": "8f21c4de-trace",
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/QueryRequest"},
                        "examples": {
                            "informasi": {
                                "summary": "Knowledge question (most common)",
                                "value": {
                                    "query": "What causes dengue fever?",
                                    "mode": "information",
                                    "options": {"format": "simple", "max_evidence": 3},
                                },
                            },
                            "keluhan": {
                                "summary": "User describes their symptoms",
                                "value": {
                                    "query": "I have had a fever for three days and a cough.",
                                    "mode": "consultation",
                                    "context": {"session_id": "chat-8812"},
                                    "options": {"format": "simple"},
                                },
                            },
                            "lanjutan": {
                                "summary": "Next turn in the same session",
                                "value": {
                                    "query": "Is that normal?",
                                    "mode": "consultation",
                                    "context": {"session_id": "chat-8812"},
                                    "options": {"format": "simple"},
                                },
                            },
                            "obat": {
                                "summary": "Medication question",
                                "value": {
                                    "query": "What are the side effects of paracetamol?",
                                    "mode": "medication",
                                    "options": {"format": "simple"},
                                },
                            },
                        },
                    }},
                },
                "responses": {
                    "200": {
                        "description": (
                            "The answer. Its shape follows `options.format`: "
                            "`simple` (answer and sources only) or `full`."
                        ),
                        "headers": {
                            "X-Request-Id": {
                                "schema": {"type": "string"},
                                "description": "Correlation id for this request.",
                            },
                            "X-Idempotent-Replay": {
                                "schema": {"type": "string", "enum": ["true"]},
                                "description": (
                                    "Present when this response was served from an "
                                    "idempotency key rather than reprocessed."
                                ),
                            },
                        },
                        "content": {"application/json": {
                            "schema": {"oneOf": [
                                {"$ref": "#/components/schemas/SimpleQueryResponse"},
                                {"$ref": "#/components/schemas/FullQueryResponse"},
                            ]},
                            "example": {
                                "answer": "Dengue fever is caused by the dengue virus, which is transmitted by mosquitoes. It can affect anyone, and in some people it becomes more severe.",
                                "sources": [{
                                    "title": "Dengue Fever: An Overview",
                                    "url": "https://doi.org/10.5772/intechopen.92315",
                                    "doi": "10.5772/intechopen.92315",
                                    "publisher": "IntechOpen",
                                    "year": 2020,
                                    "relevance": 0.553,
                                    "snippet": "Dengue fever is a disease caused by a family of viruses transmitted by mosquitoes...",
                                }],
                                "has_evidence": True,
                                "notice": None,
                                "conversation_id": None,
                                "sources_reused": False,
                                "request_id": "9f2c1a...",
                            },
                        }},
                    },
                    "400": {"description": "`query` is empty, over 5,000 characters, or `mode` is unrecognised",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"},
                                "example": {"error": "invalid_request",
                                            "detail": "Field 'query' is required."}}}},
                    "401": {"description": "The `X-API-Key` header is missing or unknown",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"}}}},
                    "429": {
                        "description": (
                            "Rate limit exceeded, per API key. The `Retry-After` header "
                            "gives the number of seconds to wait."
                        ),
                        "headers": {"Retry-After": {"schema": {"type": "integer"}}},
                        "content": {"application/json": {"example": {
                            "detail": "Request was throttled. Expected available in 42 seconds."}}},
                    },
                    "500": {"description": "Internal error. Retry once, then report it with the `request_id`",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            }
        },
        "/api/v1/intelligence/summary": {
            "post": {
                "tags": ["Conversations"],
                "summary": "Summarise a session",
                "description": (
                    "Produces a structured summary of a conversation session.\n\n"
                    "The summary contains only information that actually appeared in "
                    "the conversation; unknown fields stay `null`. Every part carries "
                    "a `provenance` value, so you can tell what the user reported, what "
                    "the system inferred, and what is evidence backed."
                ),
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["session_id"],
                            "properties": {
                                "session_id": {"type": "string", "examples": ["chat-8812"]},
                                "close_session": {
                                    "type": "boolean", "default": False,
                                    "description": "Mark the session finished. No data is deleted.",
                                },
                            },
                        },
                        "example": {"session_id": "chat-8812", "close_session": True},
                    }},
                },
                "responses": {
                    "200": {
                        "description": "Consultation summary",
                        "content": {"application/json": {
                            "schema": {"type": "object", "properties": {
                                "summary": {"$ref": "#/components/schemas/ConsultationSummary"}}},
                            "example": {"summary": {
                                "session_id": "chat-8812",
                                "chief_complaint": {"value": "fever", "provenance": "USER_REPORTED"},
                                "symptoms": [
                                    {"value": "fever", "provenance": "USER_REPORTED"},
                                    {"value": "cough", "provenance": "USER_REPORTED"},
                                ],
                                "duration": {"value": "3 days", "provenance": "USER_REPORTED"},
                                "evidence_discussed": [{"title": "Dengue Fever: An Overview",
                                                        "doi": "10.5772/intechopen.92315"}],
                                "recommended_next_step": [{
                                    "value": "Discuss these symptoms with a healthcare professional.",
                                    "provenance": "SYSTEM_GENERATED"}],
                            }},
                        }},
                    },
                    "400": {"description": "`session_id` is required"},
                    "401": {"description": "Invalid API key"},
                    "404": {"description": "Session not found"},
                },
            }
        },
        "/api/v1/intelligence/sessions/{session_id}": {
            "get": {
                "tags": ["Conversations"],
                "summary": "Retrieve session history",
                "description": "Returns every turn of the conversation and its accumulated health context.",
                "parameters": [{"name": "session_id", "in": "path", "required": True,
                                "schema": {"type": "string"}, "example": "chat-8812"}],
                "responses": {
                    "200": {"description": "Conversation history"},
                    "401": {"description": "Invalid API key"},
                    "404": {"description": "Session not found"},
                },
            },
            "delete": {
                "tags": ["Conversations"],
                "summary": "Close a session",
                "description": "Marks the session finished. Conversation data is not deleted.",
                "parameters": [{"name": "session_id", "in": "path", "required": True,
                                "schema": {"type": "string"}, "example": "chat-8812"}],
                "responses": {
                    "200": {"description": "Session closed"},
                    "404": {"description": "Session not found"},
                },
            },
        },
        "/api/v1/intelligence/capabilities": {
            "get": {
                "tags": ["Health Intelligence"],
                "summary": "Capabilities and service status",
                "description": (
                    "The modes and enum values this deployment supports. Also usable "
                    "as an availability check before sending a request."
                ),
                "responses": {"200": {"description": "Capability description"}},
            }
        },
    }
