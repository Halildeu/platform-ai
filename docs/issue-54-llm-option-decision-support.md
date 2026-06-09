# Issue #54 — LLM Option A vs B: Decision Support

Issue: `#54 [LLM API Option A vs B] yurt dışı veri akışı karar`

Status: **DECISION-SUPPORT ONLY.** The final choice is an operator + legal
decision (KVKK Madde 9 cross-border transfer + cost + Cross-AI consensus +
user approval). This document organizes the available evidence so that decision
can be made; it does **not** make or close the decision.

## The decision

Which LLM powers `meeting-ai-service` summary / decisions / action-item
extraction?

| | Option A — Cloud API | Option B — Self-host |
|---|---|---|
| Provider | OpenAI / Anthropic | Ollama + Llama 3.3 (or similar) |
| Data location | Transcript leaves the country (audio never sent) | In-country, on the GPU host |
| KVKK | Madde 9 cross-border transfer compliance required (DPA, explicit basis) | No cross-border transfer |
| Cost | Per-token API cost, no hardware | GPU/host cost (shared with STT) |
| Quality | State-of-the-art summaries | Good, model-dependent; Turkish quality to be measured |
| Ops | Managed, no model ops | Self-managed (model updates, GPU contention with STT) |
| Latency | Network round-trip | Local, but competes with STT for the GPU |

## What the codebase already provides (so this is a config decision, not a rewrite)

`meeting-ai-service` (#49) was built **backend-swappable** specifically for this
decision:

- `MAI_BACKEND` selects the backend: `mock` (default) | `anthropic` | `openai` | `ollama`.
- Anthropic/OpenAI/Ollama are stubbed (501) pending this decision; wiring the
  chosen one is a localized change, not an architecture change.
- **Key KVKK-relevant fact:** PII redaction runs in the service **before** any
  analyzer/LLM call (`MAI_REDACT_PII=true`, `redact.py` — email/TC/IBAN/phone/
  bearer/secret). So even under **Option A**, only a **redacted transcript**
  would cross the border, not raw PII. This materially narrows (but does not
  eliminate) the Madde 9 surface — the lawful-basis/DPA question still stands.

## KVKK angle (Madde 9 — yurt dışı aktarım)

- **Option A** triggers cross-border transfer of (redacted) meeting transcript
  text. Requires: explicit lawful basis, a Data Processing Agreement with the
  provider, and alignment with ADR-0030 (#52, still placeholder → ACCEPTED
  pending lawyer review). **This is a legal gate, not a technical one.**
- **Option B** keeps all text in-country → no Madde 9 transfer → lowest legal
  friction, at the cost of GPU contention and self-managed model ops.

## Cost angle

Parametric (operator supplies real numbers; see `services/live-stt-service/
scripts/cost.py` for the local-vs-cloud-per-audio-minute model):

- **Option A:** marginal per-token cost; no capex. Predictable per-meeting.
- **Option B:** amortized GPU/host cost (shared with STT — note the RTX 4070 is
  already the STT target, #40; the LLM would compete for its 8 GiB VRAM, see the
  #42 saturation data). A larger local LLM may not fit alongside large-v3.

## Quality angle

- STT WER is already measured (#43: medium 20.8%, large-v3 18.3% on CV TR) — the
  transcript quality feeding the LLM is known. Summary/decision/action quality of
  Option A vs B is **not yet measured**; a small head-to-head on a few redacted
  sample transcripts (mock harness extendable) would close this gap.

## Recommendation framing (not a decision)

- If **legal clears Madde 9** (DPA + ADR-0030 ACCEPTED) and per-meeting cost is
  acceptable → **Option A** is the fastest path to high-quality summaries, and
  redaction-before-send already limits the exposed data.
- If legal friction is high or in-country processing is mandated →
  **Option B**, accepting GPU contention with STT (may need a second GPU or
  time-sharing; revisit with #40 hardware decision).

The architecture supports either; switching is a `MAI_BACKEND` change plus the
chosen client wiring. **No code is locked by this document.**

## Inputs still required to decide (owners)

| Input | Owner | Issue |
|---|---|---|
| Madde 9 lawful basis + DPA | ⚖️ Legal | #52, #53, #60 |
| Per-meeting cost target | 🧑‍💼 Operator | — |
| Summary quality A-vs-B head-to-head | 🔬 STT (extend mock harness) | (follow-up) |
| GPU capacity for local LLM | 📊 #40 / #42 data | #40 |
| Cross-AI consensus + user approval | 🧑‍💼 Halil | #54 |

## Scope note

This document is evidence organization. It does not select a provider, send any
data anywhere, or wire a real LLM backend. Branch is on the contributor fork
only; no upstream push.
