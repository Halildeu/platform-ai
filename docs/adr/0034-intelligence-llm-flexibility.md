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
summary grounding (lexical proxy for faithfulness) + action-item
precision/recall ≥ target + every output carries a citation.

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

- A tenant switches deployment mode by config (`MAI_BACKEND`), not a code change;
  self-host is one mode among several, satisfying the "tek mod yapma" requirement.
- G-INT is measurable end-to-end: CPU/mock for logic in CI, and real numbers via
  `intel_eval.py` against `ollama` on the RTX 4070 (measured 2026-06-17, above).
  The remaining gap to full ACCEPTED is a *real-meeting* pilot (not synthetic) to
  calibrate absolute action precision/recall.
- Timestamped citations: `Citation` carries the source-sentence char span plus an
  optional `start_sec`. When `analyze()` is given STT `segments` (Whisper-style
  `{text,start}`), each citation is stamped with its segment's wall-clock start;
  without timing it stays `None` (backward compatible). The char span remains the
  stable join key.

## Status promotion criteria

Synthetic G-INT numbers are in (2026-06-17, `llama3.1:8b`). Promote to **ACCEPTED**
when `intel_eval.py` has been run on a **real-meeting** transcript (consent +
neutral content, recording imha'd after measurement) and grounding rate +
decision/action precision/recall meet the G-INT target — ideally with a semantic
faithfulness check (entailment), not lexical-only. Evidence file:
`docs/evidence/intel-eval-<date>.jsonl`.
