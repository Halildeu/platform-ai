# meeting-ai-service

Faz 24 Meeting Intelligence — meeting summary / decisions / action items (skeleton, #49).

FastAPI service: **transcript → summary + decisions + action items**.

> **KVKK boundary:** the transcript is **redacted before any analyzer/LLM call**
> (`MAI_REDACT_PII=True` by default). Even a real LLM backend only ever receives
> redacted text. Raw transcript is never logged.

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
