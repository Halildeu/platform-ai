# meeting-ai-service

Faz 24 Meeting Intelligence — meeting summary / decisions / action items (skeleton, #49).

FastAPI service: **transcript → summary + decisions + action items**.

> **KVKK boundary:** the transcript is **redacted before any analyzer/LLM call**
> (`MAI_REDACT_PII=True` by default). The post-meeting `/ask` path also redacts the
> question before prompt construction. Even a real LLM backend only ever receives
> redacted text. Raw transcript/question text is never logged. Redaction is
> **fail-closed** (ADR-0043 D3): if a broad residual detector still finds a PII
> shape after redaction, the request is **blocked with 422** rather than sent.
> `/ask` is also fail-closed for hallucination exposure: if a generated answer
> cannot be grounded to a transcript sentence, the unsupported prose is withheld
> and the response uses the fixed answer `Metinde bu bilgi yok.` with
> `grounded=false`.

## Citation grounding & hallucination guard (ADR-0043 D4/D8.1)

The product wedge for regulated buyers — and the gap **no competitor fills** (Otter /
Fireflies / Fathom / Copilot / Granola / Gong all do timestamp-linking + human review,
none machine-check claim↔span consistency): every shipped decision/action is checked
against the transcript with **deterministic contradiction gates**, not merely overlap.

> **Honest scope (v1):** this is **verified span-grounding with deterministic
> contradiction gates**, NOT full NLI entailment. It is model-free / CPU-only; it
> high-precision-FAILs the cases overlap and embedding-cosine miss (negation/number),
> but it does not prove positive entailment. The **`summary` is also exposure-guarded**:
> only summary sentences that pass the same transcript-span guard are returned.
> Unsupported summary prose is withheld and tracked through
> `rejected_claims[].kind=summary`; `summary_grounding_status` is
> `verified`, `partial_verified`, `withheld`, or `empty`. When fully withheld,
> `summary` is an empty string; clients should render any fallback copy from the
> status field rather than treating a static API string as meeting data.
> Entity-NER + embedding-backed semantic summary grounding are roadmap.

A claim is `PASSED` (shipped) only if its best-matching sentence survives a layered,
CPU-only, zero-model verifier (`app/services/citation.py`):

1. **content-word coverage** (necessary, not sufficient);
2. **polarity/negation gate** — "reddedildi" cited to "onaylandı" has high overlap but
   opposite meaning → rejected (the failure mode lexical/embedding scores miss);
3. **number/quantity gate** — "%20" cited to "%12" → rejected;
4. **span-informativeness** — a generic filler span ("Tamam.") can't ground a decision.
5. **owner attribution** — an action assignee is shown only when that owner appears in
   the same cited sentence; otherwise the action is kept with `owner=null` and the
   unsupported assignment is recorded as `rejected_claims[].kind=action_owner`.

Verdicts are 3-way (`PASSED` / `FAILED` / `LOW_CONFIDENCE`); only `PASSED` reaches the
user-visible `summary`, `decisions`, or `action_items`. **Ungrounded/contradicted
claims are withheld** into `rejected_claims` (auditable, never presented as fact —
ADR-0043 D8.1 fail-closed). Each citation carries a hash/offset key
(`source_char_start/end`, `source_hash`, `quote_hash`) pinning it to the exact
transcript span. Summary citations live in `summary_citations`; decision/action
citations live in `citations`.

## G-INT evidence gate

`scripts/intel_eval.py` produces one metadata row per model/seed run. The row now
includes explicit `dataset_kind`; default is `synthetic-neutral`. Real #162 acceptance
requires an approved pilot class (`pilot-meeting`, `workcube-pilot`, or
`customer-pilot`) plus explicit thresholds checked by `scripts/gint_gate.py`.
Pilot rows must also use a real backend and a non-fixture eval-set path; editing a
synthetic row's `dataset_kind` field is not enough to pass the gate.

Synthetic fixtures and mock runs are useful for CI and bakeoffs, but the gate refuses
to let them satisfy G-INT acceptance. Evidence rows must stay metadata-only: raw
transcripts, expected actions/decisions, prompts, responses, source quotes, citations
and PII-shaped values are rejected by the verifier.

Current synthetic evidence is expected to remain blocked:

```bash
python scripts/gint_gate.py \
  --gint-evidence ../../docs/evidence/intel-eval-2026-06-17.jsonl \
  --min-grounding-rate 0.95 \
  --min-action-precision 0.80 \
  --min-action-recall 0.80 \
  --min-decision-precision 0.75 \
  --min-decision-recall 0.75 \
  --max-schema-invalid-rate 0 \
  --max-format-invalid-rate 0 \
  --max-backend-error-rate 0 \
  --max-truncation-risk-rate 0 \
  --min-samples 3
```

For an approved pilot run:

```bash
MAI_BACKEND=ollama python scripts/intel_eval.py \
  --eval-set C:/faz24-pilot/intel-pilot-2026-06-25.json \
  --dataset-kind pilot-meeting \
  --tag ollama-pilot
```

## Backends

| `MAI_BACKEND` | Behaviour |
|---|---|
| `mock` (default) | Deterministic keyword-based extractive analysis — no LLM, no key, unit-tested |
| `anthropic` / `openai` | Option A real LLM — **stub** (501); wiring needs ADR-0030 Option A/B + API key |
| `ollama` | Option B local LLM through Ollama `/api/generate`; intended on-prem |

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
