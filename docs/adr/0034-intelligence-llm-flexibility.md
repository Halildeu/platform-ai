# ADR-0034: Intelligence LLM Backend Flexibility

- Status: EVIDENCE-BACKED PROVISIONAL — G-INT NUMBERS PENDING (GPU pilot)
- Date: 2026-06-17
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
- G-INT **logic** is testable on CPU today (`scripts/intel_eval.py`, mock backend:
  faithfulness + action precision/recall/F1 + citation coverage). Real target
  numbers require running the same runner against `ollama` on the RTX 4070 GPU PC
  (Kartal) — that is the only remaining step before this ADR is fully ACCEPTED.
- Timestamped citations: `Citation` currently carries the source-sentence char
  span (`start_char`/`end_char`). Mapping to wall-clock timestamps is deferred
  until the transcript carries STT word timings; the char span is the stable join
  key for that later mapping.

## Status promotion criteria

Promote to **ACCEPTED** when `intel_eval.py` has been run with `MAI_BACKEND=ollama`
on real meeting transcripts and faithfulness + action precision/recall meet the
G-INT target. Evidence file: `docs/evidence/intel-eval-<date>.jsonl`.
