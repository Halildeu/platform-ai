# ADR-0033: Diarization Approach (pyannote vs alternative)

- Status: **DRAFT — EVIDENCE PENDING (do NOT promote to ACCEPTED yet)**
- Date: 2026-06-17
- Issue: `#161 [Faz24 T-B] STT kalite kanıtı — Türkçe WER + diarization [P0]`
- Decision scope: which diarization approach the product commits to, and where it
  runs (live vs post-processing), before real-meeting DER evidence exists.

> **Discipline note (why this is still DRAFT).** A previous draft (PR #163) was
> withdrawn on purpose: *"önce ÖLÇÜM, sonra karar ADR'si — maintainer'a veri
> olmadan model seçimi sorulamaz"* (the #35 WER matrix → ADR-0031 order was
> correct). This file therefore records the **candidate matrix, criteria, and the
> measurement harness**, with measured cells filled only as the GPU sweep
> produces them. It must not be promoted to ACCEPTED until the pyannote sweep
> (n>1) and at least one measured alternative exist, and the real-meeting DER
> (G-WER gate) is calibrated by a pilot.

## Context

Diarization ("who spoke when") is an industry-standard meeting-intelligence
feature the product currently lacks. The #161 wedge is **Turkish quality**, so
the diarization choice must be justified on measured Turkish performance, not on
a model's reputation. Two hard constraints frame the decision:

1. **GPU is 8 GB** (RTX 4070; the "12 GB" in older capacity notes is wrong —
   `nvidia-smi` reports 8188 MiB; finding carried from PR #163). STT
   (`medium-int8` live + `large-v3-turbo` final, ADR-0031) plus Ollama already
   press 8 GB. Diarization therefore cannot run as a third concurrent live model
   → it is a **post-processing / batch** step, not a live path.
2. **KVKK (ADR-0030):** diarization emits ANONYMOUS `SPEAKER_xx` labels only.
   **No voiceprint / biometric enrolment** in this phase. Speaker→person linking
   is a contextual, human-confirmed overlay (see "Speaker → person" below).

## Candidate Matrix

`PENDING` = not yet measured under the reproducible fixture + RTX 4070 protocol
(`scripts/diar_matrix.py`). Measured cells cite `docs/pr-diar-01-line-161-matrix-report.md`.

| Candidate | License | Gated model? | TR DER | Peak VRAM | RTF | Notes |
|---|---|---|---|---|---|---|
| **pyannote 3.1** (primary) | MIT (model: gated, free) | yes (HF token) | **50.14%** (n=6, synthetic) | 2155 MB | 0.024 | end-to-end pipeline; wired + measured |
| **speechbrain ECAPA** (alternative) | Apache-2.0 | **no** (free) | PENDING (harness wired, GPU run pending) | PENDING | PENDING | energy-VAD + ECAPA embeddings + cosine clustering |
| NeMo | Apache-2.0 | no | PENDING (adapter not wired) | PENDING | PENDING | heavier install; candidate only if pyannote fails target |
| Cloud (Azure/Google) | commercial | n/a | not measured | n/a | n/a | **m.9 cross-border → ADR-0030 boundary**; rejected unless on-prem fails |
| VAD-only fallback | — | no | n/a (no speaker sep.) | minimal | minimal | degraded fallback if no GPU model fits |

Both running backends share one harness so the comparison is apples-to-apples
(same fixtures, same DER scorer). The alternative requirement of #161
("pyannote vs alternatif") is satisfied by the **speechbrain** column once its
GPU run lands; NeMo/cloud are compared here on license/VRAM/KVKK criteria.

## Speaker → person mapping

`app/services/speaker_mapping.py` (no biometrics):
- `summarize_speakers` — contextual facts per anonymous label (talk time, turns,
  first-seen);
- `apply_mapping` — overlays a **human-confirmed** `{SPEAKER_00: "Ayşe"}` mapping
  while keeping the anonymous label canonical (reversible);
- `suggest_mapping` — best-effort ordering heuristic vs a known attendee roster,
  explicitly a suggestion to be human-reviewed, never automatic identification.

## Decision (PROVISIONAL — pending evidence)

1. **Placement:** diarization runs as **post-processing batch**, not a live model
   (8 GB constraint). This part is firm regardless of backend choice.
2. **Backend:** **pyannote 3.1 is the provisional primary**, with **speechbrain
   ECAPA as the measured alternative**. Final selection is deferred until the
   sweep (n>1) and the speechbrain run produce comparable DER/VRAM/RTF.
3. **Identity:** anonymous labels canonical; names only via human-confirmed
   overlay (KVKK).

## Explicit Non-Decision

- No backend is locked. A single n=1 smoke DER (45.04%) is **not** a decision-grade
  number — it reflects a synthetic, overlap-free fixture (pyannote may misestimate
  speaker count) and one sample.
- No DER target is asserted yet; it is set with the pilot (G-WER gate).

## Reopen / Promote-to-ACCEPTED Triggers

Promote only when ALL hold:
- pyannote sweep (n≥5, mixed 2/3-speaker fixtures) recorded;
- speechbrain alternative measured on the same fixtures;
- a real-meeting (pilot) DER calibrates absolute numbers (gated on go-live #59 /
  consent, like ADR-0031's pilot leg);
- chosen backend meets the agreed diarization DER target (G-WER).

## Cross-AI Consensus

Required, not yet complete: this DRAFT (Cursor Fable 5) + independent reviewer +
human/operator approval all pending.

## Consequences

Positive: candidate matrix + criteria + measurement harness are explicit; the
8 GB and KVKK constraints are recorded; an alternative is genuinely measurable,
not hand-waved.

Negative: backend choice stays provisional until the GPU sweep + alternative +
pilot land; absolute DER cannot be claimed yet.
