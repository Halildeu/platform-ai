# ADR-0034: Intelligence LLM Backend Flexibility

- Status: EVIDENCE-BACKED PROVISIONAL — REAL-MEETING PILOT PENDING
- Date: 2026-06-17 (G-INT evidence: 2026-06-17, RTX 4070)
- Issue: `#162 [Faz24 T-C] Intelligence — LLM özet/karar/aksiyon + kaynaklı çıktı` (PR-time ADR)
- Decision scope: how the Intelligence layer (summary/decisions/actions + ask-AI)
  selects and isolates its LLM backend.

## Context

#162 requires the product's core value (özet/karar/aksiyon) **with citations**
("AI dedi" değil "şu cümleden çıkarıldı"). The 2-AI istişare (Claude + Codex
`019ed1f5`) concluded that for a regulated (KVKK) sector the differentiator is
**grounded output**, not raw summarization. The acceptance gate is **G-INT**:
summary grounding (lexical proxy for faithfulness) + metadata-only summary
verified rate + every shipped output carrying a PASSED citation + action-item
precision/recall ≥ target.

A regulated customer cannot be forced onto a single deployment mode: some
tenants require fully on-prem self-host (no data leaves the cluster), others
accept a private-cloud / transcript-only LLM. The issue is explicit: *"self-host'u
tek mod yapma"* — do not make self-host the only mode.

## G-INT Evidence (2026-06-17 — RTX 4070, 8 synthetic neutral meetings)

`scripts/intel_eval.py` run against both locally-hosted Ollama models, redaction
on, `format=json`. Evidence: `docs/evidence/intel-eval-2026-06-17.jsonl`.

> **Metric honesty (review #166).** "Grounding rate" is *lexical* — the fraction
> of claims whose tokens overlap a transcript sentence above threshold. It is a
> hallucination **floor** (catches fabricated claims), NOT semantic faithfulness:
> a claim that reuses words but inverts meaning ("onaylandı"→"reddedildi") still
> counts as grounded. Action/decision P/R use one-to-one token-overlap matching
> on a synthetic set. Real semantic faithfulness (entailment/NLI) and absolute
> P/R calibration await a real-meeting pilot.

Reproducible run (one-to-one matcher + decision scoring, direct `intel_eval.py`
output — `docs/evidence/intel-eval-2026-06-17.jsonl`):

| Model | Grounding rate | Action P / R | Decision P / R | p50 |
|---|---|---|---|---|
| **llama3.1:8b** | 81.2% | 50% / 50% | 37.5% / 31.2% | 12.5 s |
| qwen2.5:7b | 77.1% | 43.8% / 50% | 31.2% / 31.2% | 15.5 s |

> **Single-shot variance — read with care.** An earlier llama3.1 run scored
> grounding 95.8%; this run 81.2%. The models are non-deterministic and n=8 is
> tiny, so absolute numbers swing run-to-run and the two models are closer than a
> single run suggests. **No "clear winner" claim is justified from this set.**
> What holds: grounding stays the highest-signal metric, llama3.1 is marginally
> ahead and is a reasonable self-host default, and P/R are low enough that
> prompt/extraction work is needed. A trustworthy decision needs a **real-meeting
> pilot with multiple seeds**, not this synthetic single-shot.

## Decision

The Intelligence layer is **backend-swappable behind one interface**, selected by
`MAI_BACKEND`:

| Backend | Mode | Use |
|---|---|---|
| `mock` | deterministic, no LLM | CI / CPU unit tests / G-INT logic |
| `ollama` | on-prem self-host | KVKK tenants — no data leaves the cluster |
| `anthropic` / `openai` | private-cloud / transcript-only | tenants that accept it |

Hard constraints, enforced in code (`app/core/config.py`):

1. **Redaction-before-LLM is mandatory for every non-mock backend.**
   `MAI_REDACT_PII=False` is rejected unless `backend == "mock"` — PII is redacted
   before any analyzer/LLM call. This is the KVKK boundary and cannot be disabled
   for a real backend.
2. **Citation/grounding is backend-independent.** `ground_claims` (token-overlap
   hallucination guard) runs on the redacted transcript regardless of backend, so
   the G-INT citation requirement holds for self-host and cloud alike.
3. **Transcript is never logged** — only lengths/metadata/correlation-id.

### Network boundary — deployment contract (not app-level)

The "no data leaves the cluster" guarantee for `ollama` is enforced at the
**network layer, not in application code** (review #166, Codex MAJOR). An app-level
allowlist on `ollama_host` is weak security (a misconfigured DNS name passes a
string check) and gives false assurance. The contract instead:

- `ollama_host` MUST resolve to a cluster-local / on-prem endpoint; egress to
  the public internet from the Intelligence pod MUST be blocked by a Kubernetes
  `NetworkPolicy` (default-deny egress + allow only the in-cluster Ollama
  service). This is the actual KVKK enforcement point.
- `anthropic` / `openai` backends are the *explicit* "data may leave" modes,
  chosen per tenant with consent — they are not accidents to be guarded against
  but deliberate deployment choices.

Tracking: the NetworkPolicy lives in platform-k8s-gitops (deploy-time), not in
this service. This ADR records the requirement; the manifest is its enforcement.

## Consequences

### 2026-09-13 TEST Semantic Profile (#3753)

The existing deployment interface remains on-prem Ollama. A TEST-only candidate
selects the already installed `qwen2.5:14b` artifact
`7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6`
through the protected model/digest pair, not task XML, a global environment
override, or a changed production default. The default remains `llama3.1:8b`.
This is a measured model/prompt PoC; real-meeting pilot status above is unchanged.

The frozen 12-case synthetic sentence-label corpus has 9 decision and 11 action
labels. Baseline llama with the previous prompt scored decision TP/FP/FN 4/1/5,
action 8/5/3, and exact action plus owner/date 4/9/7. The candidate's contrast
prompt, schema-constrained selection and qwen14 scored decisions 9/1/0 and
actions including metadata 11/0/0. The separately authored six-case challenge
scored decisions 4/0/0 and actions including metadata 9/1/0. Both meet the fixed
project regression targets (precision >=0.90, recall >=0.85, zero execution
errors), not an industry-wide accuracy standard. Source citation fidelity is
reported separately from class precision/recall. Prior failed model/prompt
trials are retained; labels and thresholds were not relaxed to pass a candidate.

Known residuals remain explicit: a no-new-decision statement was selected as a
decision, and the cancellation challenge retained one extra action. The small
visible synthetic sets do not establish human meeting accuracy, calibrate a
semantic confidence score, or authorize automatic execution of extracted tasks.
Do not call a source-similarity badge semantic confidence. The actual customer
acceptance still requires fresh TEST recording, persisted result, authenticated
reopen/citation navigation and negative authorization checks on exact artifacts.

The source runtime now checks the selected model's digest before and after both
analysis and follow-up-question generation. Missing/ambiguous/mismatched model
inventory fails closed, and readiness checks the selected model rather than just
an HTTP 200 from Ollama. This detects ordinary mutable-tag replacement; it is
not an atomic model-registry lock. Checks share the existing request budget.
Windows model and expected digest must be supplied together; unrelated configure
and consumer disable preserve the pair and the existing timeout/lease values.
Before rolling back to older code that does not recognize these keys, restore
the protected compatible original config before restarting the old revision,
then issue a fresh permit for its exact host/producer identity.

See [semantic evaluation](../../services/meeting-ai-service/docs/semantic-evaluation.md)
for labels, reproducible commands, evidence level and project thresholds.
The structured-output transport follows the official
[Ollama schema contract](https://docs.ollama.com/capabilities/structured-outputs),
and model inventory comes from [the tags API](https://docs.ollama.com/api/tags).
Neither protocol guarantees semantic correctness; that is why separate labeled
regressions and real user acceptance are required.

- A tenant switches deployment mode by config (`MAI_BACKEND`), not a code change;
  self-host is one mode among several, satisfying the "tek mod yapma" requirement.
- G-INT is measurable end-to-end: CPU/mock for logic in CI, and real numbers via
  `intel_eval.py` against `ollama` on the RTX 4070 (measured 2026-06-17, above).
  The remaining gap to full ACCEPTED is a *real-meeting* pilot (not synthetic) to
  calibrate absolute action precision/recall.
- Timestamped citations: the wire `Citation` exposes `source_index` + optional
  `start_sec`; the source-sentence **char span is internal** (the `Sentence`
  dataclass in `citation.py`), not part of the HTTP schema. When `analyze()` is
  given STT `segments` (Whisper-style `{text,start}`), each citation is stamped
  with its segment's wall-clock start; without timing it stays `None` (backward
  compatible). Internally, that sentence char span is the stable join key for
  the timing map.

## Status promotion criteria

Synthetic G-INT numbers are in (2026-06-17, `llama3.1:8b`). Promote to **ACCEPTED**
when `intel_eval.py` has been run on a **real-meeting** transcript (consent +
neutral content, recording imha'd after measurement) and grounding rate,
citation coverage, summary verified rate, and decision/action precision/recall
meet the G-INT target — ideally with a semantic faithfulness check (entailment),
not lexical-only.

Acceptance evidence now has two files:

1. `docs/evidence/intel-eval-<date>.jsonl` — metadata-only `intel_eval.py` rows
   with explicit `dataset_kind=pilot-meeting|erp-crm-pilot|customer-pilot`,
   real backend, and non-fixture eval-set path.
2. `docs/evidence/gint-gate-<date>.json` — `scripts/gint_gate.py` verifier output
   with `status=pass` against explicit G-INT thresholds.

Synthetic/mock rows remain valid bakeoff evidence, but `gint_gate.py` blocks them
from satisfying the real pilot acceptance gate.
