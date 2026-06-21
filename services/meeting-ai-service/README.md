# meeting-ai-service

Faz 24 Meeting Intelligence — meeting summary / decisions / action items (skeleton, #49).

FastAPI service: **transcript → summary + decisions + action items**.

> **KVKK boundary:** the transcript is **redacted before any analyzer/LLM call**
> (`MAI_REDACT_PII=True` by default). Even a real LLM backend only ever receives
> redacted text. Raw transcript is never logged. Redaction is **fail-closed**
> (ADR-0043 D3): for a real LLM backend, if a broad residual detector still finds a
> PII shape after redaction, the request is **blocked with 422** rather than sent.

## Citation grounding & hallucination guard (ADR-0043 D4/D8.1)

The product wedge for regulated buyers — and the gap **no competitor fills** (Otter /
Fireflies / Fathom / Copilot / Granola / Gong all do timestamp-linking + human review,
none machine-check entailment): every shipped decision/action is **verified against the
transcript**, not merely overlapping.

A claim is `PASSED` (shipped) only if its best-matching sentence survives a layered,
CPU-only, zero-model verifier (`app/services/citation.py`):

1. **content-word coverage** (necessary, not sufficient);
2. **polarity/negation gate** — "reddedildi" cited to "onaylandı" has high overlap but
   opposite meaning → rejected (the failure mode lexical/embedding scores miss);
3. **number/quantity gate** — "%20" cited to "%12" → rejected;
4. **span-informativeness** — a generic filler span ("Tamam.") can't ground a decision.

Verdicts are 3-way (`PASSED` / `FAILED` / `LOW_CONFIDENCE`); only `PASSED` reaches the
user-visible `decisions`/`action_items`. **Ungrounded/contradicted claims are withheld**
into `rejected_claims` (auditable, never presented as fact — ADR-0043 D8.1 fail-closed).
Each citation carries a hash/offset key (`source_char_start/end`, `source_hash`,
`quote_hash`) pinning it to the exact transcript span.

## Backends

| `MAI_BACKEND` | Behaviour |
|---|---|
| `mock` (default) | Deterministic keyword-based extractive analysis — no LLM, no key, unit-tested |
| `anthropic` / `openai` | Option A real LLM — **stub** (501); wiring needs ADR-0030 Option A/B + API key |
| `ollama` | Option B local LLM — **stub** (501); wiring needs an Ollama host |

## API

- `POST /analyze` — JSON `{transcript, meeting_id?, session_id?}` → `AnalyzeResponse`
- `GET /health`, `GET /metrics` (`mai_*`, `kvkk_*`)

## Run (skeleton)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --port 8400
```

```bash
curl -X POST http://localhost:8400/analyze \
  -H "Content-Type: application/json" \
  -d '{"transcript":"Toplantıda X kararlaştırıldı. Ali raporu hazırlayacak."}'
```

## Config (env, prefix `MAI_`)

`MAI_BACKEND`, `MAI_MODEL_NAME`, `MAI_MAX_TRANSCRIPT_CHARS`, `MAI_REDACT_PII`,
`MAI_REQUEST_TIMEOUT`, `MAI_SUMMARY_MAX_CHARS`. See `app/core/config.py`.

## Follow-ups (out of #49 skeleton scope)

- ADR-0030 Option A/B decision (cloud LLM vs local Ollama) + secret handling.
- Wire the chosen real LLM backend behind `MAI_BACKEND`.
- Prompt design for TR summary/decision/action extraction.
- Consume `live-stt`/`final-stt` + `diarization` outputs (speaker-attributed actions).
