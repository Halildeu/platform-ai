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
> Relative-date normalization, Entity-NER + embedding-backed semantic summary
> grounding are roadmap. Response contract `schema_version=5-adr0043` adds
> fail-closed fact-fusion semantics: one cited sentence must cover the material
> claim; separate facts merged from different transcript sentences are withheld.

A claim is `PASSED` (shipped) only if its best-matching sentence survives a layered,
CPU-only, zero-model verifier (`app/services/citation.py`):

1. **content-word coverage** (necessary, not sufficient);
2. **single-source materiality** — a decision/action/summary sentence that merges
   supported prose with extra unsupported facts is rejected unless one cited source
   sentence covers that material;
3. **polarity/negation gate** — "reddedildi" cited to "onaylandı" has high overlap but
   opposite meaning → rejected (the failure mode lexical/embedding scores miss);
4. **number/quantity gate** — "%20" cited to "%12" → rejected;
5. **span-informativeness** — a generic filler span ("Tamam.") can't ground a decision.
6. **owner attribution** — an action assignee is shown only when that owner appears in
   the same cited sentence; otherwise the action is kept with `owner=null` and the
   unsupported assignment is recorded as `rejected_claims[].kind=action_owner`.
7. **due-date attribution** — an action `due_date` is shown only when the same cited
   sentence contains the copied relative phrase (`cuma`, `yarın`, etc.) or copied
   numeric date/time text. Unsupported, reformatted, or normalized dates are nulled
   and recorded as `rejected_claims[].kind=action_due_date`.

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
requires an approved pilot class (`pilot-meeting`, `erp-crm-pilot`, or
`customer-pilot`) plus explicit thresholds checked by `scripts/gint_gate.py`.
Pilot rows must also use a real backend, a non-fixture eval-set path, and full
`sha256:<64 hex>` `eval_set_hash`, `prompt_hash`, `sample_manifest_hash`, and
`sample_count_hash` values; editing a synthetic row's `dataset_kind` field or
only changing `n_samples` is not enough to pass the gate. `sample_manifest_hash`
is a private eval-set fingerprint, and `sample_count_hash` binds the declared
sample count to the eval-set hash without emitting transcript text or labels.
G-INT rows also carry metadata-only `citation_coverage` and
`summary_verified_rate`: a pilot cannot pass merely because decisions/actions
look grounded while shipped outputs lack PASSED citations or summaries are
silently withheld.

Synthetic fixtures and mock runs are useful for CI and bakeoffs, but the gate refuses
to let them satisfy G-INT acceptance. Evidence rows must stay metadata-only: raw
transcripts, expected actions/decisions, prompts, responses, source quotes, citations
and PII-shaped values are rejected by the verifier.

Current synthetic evidence is expected to remain blocked:

```bash
python scripts/gint_gate.py \
  --gint-evidence ../../docs/evidence/intel-eval-2026-06-17.jsonl \
  --min-grounding-rate 0.95 \
  --min-citation-coverage 1.0 \
  --min-summary-verified-rate 0.90 \
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

## Durable analysis-result delivery (#247)

When `MAI_INGESTION_ENABLED=true`, a successful `/analyze` call is committed to an
encrypted local SQLite-WAL outbox before the HTTP response returns. Network delivery
to meeting-service runs in a lifespan worker, so Keycloak/meeting-service latency is
not added to the user-visible analysis path. The same `analysisRunId` is reused for
every attempt and sent as `Idempotency-Key`; `200` replay and `201` create both ACK
the local row. Retryable network/401/429/5xx results use exponential backoff and
jitter. Terminal 4xx or the attempt limit moves the encrypted row to DLQ.

The outbox contains no raw transcript. It stores the transcript SHA-256 and the
already-redacted analysis payload. That payload is still sensitive and is encrypted
with AES-256-GCM. No plaintext fallback or process-generated key exists.

Required configuration when enabled:

- `MAI_MEETING_SERVICE_BASE_URL`
- `MAI_MEETING_SERVICE_TOKEN_URL`
- `MAI_MEETING_SERVICE_CLIENT_ID`
- `MAI_MEETING_SERVICE_CLIENT_SECRET` (Vault/ESO or host secret injection; on the
  Windows GPU host this is materialized only in process from a DPAPI LocalMachine
  blob by `deploy/gpu-host/meeting-ai-runtime-env.ps1`)
- `MAI_INGESTION_STORE_PATH` (absolute, local persistent disk; not NFS/SMB)
- `MAI_INGESTION_ACTIVE_KEY_ID`
- `MAI_INGESTION_ENCRYPTION_KEYS_JSON` — secret JSON keyring, values are base64
  encoded 32-byte AES keys, for example `{"2026-q3":"<base64>"}`

Key rotation is additive: inject old + new keys, switch `ACTIVE_KEY_ID` to the new
key, drain/requeue old rows, then remove the old key. Startup fails closed while any
row references a missing key. The current SQLite adapter supports multiple worker
processes on one local filesystem via `BEGIN IMMEDIATE` + leases; horizontal
multi-host execution requires replacing the store adapter with shared PostgreSQL,
Kafka, or durable Redis rather than putting SQLite on network storage.

Metadata-only health is exposed under `/health.analysis_delivery`; Prometheus emits
queue depth, oldest age, enqueue, retry, delivered/replayed, and DLQ counters. To
inspect bounded DLQ metadata and requeue one operator-reviewed row without printing
payload content:

```bash
cd services/meeting-ai-service
python scripts/requeue_analysis_delivery.py --list-dead --limit 100
python scripts/requeue_analysis_delivery.py --analysis-run-id <uuid>
```

Service credential rotation is a secret-injection operation: publish the replacement
client secret through Vault/ESO (or the Windows host secret channel), restart the
service so settings are refreshed, and monitor the queue/DLQ until delivery catches
up. A revoked credential is never persisted; repeated `401` attempts retain the
encrypted payload and eventually move it to DLQ for explicit operator requeue.

The Windows host channel is non-executable and fail-closed. Use elevated
`deploy/gpu-host/configure-meeting-ai.ps1`; it prompts with `SecureString`, stores
the client secret and AES keyring as DPAPI LocalMachine ciphertext under a
SYSTEM/Administrators-only ACL, writes by same-volume atomic replace, and keeps old
encryption keys during additive rotation. Plaintext `.ps1` secret overrides are not
supported for meeting-ai ingestion.

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
