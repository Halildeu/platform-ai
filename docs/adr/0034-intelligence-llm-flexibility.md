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
summary faithfulness + action-item precision/recall ≥ target + every output
carries a citation.

A regulated customer cannot be forced onto a single deployment mode: some
tenants require fully on-prem self-host (no data leaves the cluster), others
accept a private-cloud / transcript-only LLM. The issue is explicit: *"self-host'u
tek mod yapma"* — do not make self-host the only mode.

## G-INT Evidence (2026-06-17 — RTX 4070, 8 synthetic neutral meetings)

`scripts/intel_eval.py` run against both locally-hosted Ollama models, redaction
on, `format=json`. Evidence: `docs/evidence/intel-eval-2026-06-17.jsonl`.

| Model | Faithfulness | Action P / R | F1 | p50 |
|---|---|---|---|---|
| **llama3.1:8b** | **95.8%** | 68.8% / 62.5% | 58.3% | 13.4 s |
| qwen2.5:7b | 72.9% | 25.0% / 37.5% | 25.0% | 13.7 s |

**Self-host default = `llama3.1:8b`.** It is markedly more faithful (95.8% vs
72.9% — qwen fabricates decisions/actions absent from the transcript on this
set) and stronger on action extraction. The faithfulness result confirms the
core G-INT claim — grounded, non-hallucinated output — holds on a real on-prem
LLM. Action precision/recall is mid-range and measured on a *synthetic* set with
strict token-overlap matching; absolute calibration awaits a real-meeting pilot.

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

## Consequences

- A tenant switches deployment mode by config (`MAI_BACKEND`), not a code change;
  self-host is one mode among several, satisfying the "tek mod yapma" requirement.
- G-INT is measurable end-to-end: CPU/mock for logic in CI, and real numbers via
  `intel_eval.py` against `ollama` on the RTX 4070 (measured 2026-06-17, above).
  The remaining gap to full ACCEPTED is a *real-meeting* pilot (not synthetic) to
  calibrate absolute action precision/recall.
- Timestamped citations: `Citation` currently carries the source-sentence char
  span (`start_char`/`end_char`). Mapping to wall-clock timestamps is deferred
  until the transcript carries STT word timings; the char span is the stable join
  key for that later mapping.

## Status promotion criteria

Synthetic G-INT numbers are in (2026-06-17, `llama3.1:8b`). Promote to **ACCEPTED**
when `intel_eval.py` has been run on a **real-meeting** transcript (consent +
neutral content, recording imha'd after measurement) and faithfulness + action
precision/recall meet the G-INT target. Evidence file:
`docs/evidence/intel-eval-<date>.jsonl`.
