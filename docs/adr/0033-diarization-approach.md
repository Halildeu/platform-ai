# ADR-0033: Diarization Approach (pyannote vs alternative)

- Status: **DRAFT — EVIDENCE PENDING (do NOT promote to ACCEPTED yet)**
- Amended by: **ADR-0035 (2026-06-18)** — voiceprint constraint updated (KVKK m.6, legal-gated)
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
2. **KVKK (ADR-0030):** diarization emits ANONYMOUS `SPEAKER_xx` labels by
   default. **Amended by ADR-0035 (Karar 2, 2026-06-18):** voiceprint / biometric
   enrolment is now **approved** for automatic speaker identification — but it is
   biometric / special-category data (KVKK m.6), so **live processing is GATED on
   the legal track** (explicit-consent framework + VERBİS/aydınlatma +
   retention/erasure policy + opt-out fallback to manual labelling) — tracked in
   **#168**. Code may be written; real-voiceprint processing must NOT go live
   until that gate clears.
   Until then, speaker→person linking stays anonymous + human-confirmed (see
   "Speaker → person" below).

## Candidate Matrix

`PENDING` = not yet measured under the reproducible fixture + RTX 4070 protocol
(`scripts/diar_matrix.py`). Measured cells cite `docs/pr-diar-01-line-161-matrix-report.md`.

| Candidate | License | Gated model? | TR DER | Peak VRAM | RTF | Notes |
|---|---|---|---|---|---|---|
| **pyannote 3.1** | MIT (model: gated, free) | yes (HF token) | 47.8%¹ | 2155 MB | 0.024 | end-to-end pipeline |
| **speechbrain ECAPA** | Apache-2.0 | **no** (free) | 54.6%¹ | **307 MB** | **0.003** | 7× less VRAM, faster |
| NeMo | Apache-2.0 | no | PENDING (adapter not wired) | PENDING | PENDING | heavier install; candidate only if pyannote fails target |
| Cloud (Azure/Google) | commercial | n/a | not measured | n/a | n/a | **m.9 cross-border → ADR-0030 boundary**; rejected unless on-prem fails |
| VAD-only fallback | — | no | n/a (no speaker sep.) | minimal | minimal | degraded fallback if no GPU model fits |

> ¹ **synthetic-smoke, NOT a baseline DER (review #164).** Measured at the
> dscore-standard `collar=0.25` (n=6) on `2026-06-17`, but on a synthetic fixture
> whose per-speaker clips repeat byte-identically and never overlap. They measure
> mainly over-segmentation, **not speaker confusion or overlap** — the hardest,
> most product-relevant part of DER. (For reference, collar=0 gave 50.1% / 56.6%;
> the collar moved both ~2pt but not the ordering.) A realistic fixture (distinct,
> non-identical audio + overlap) and a real-meeting pilot will move absolute DER
> and the pyannote↔speechbrain gap. **No ranking claim is drawn from these cells**;
> they prove the harness runs and is deterministic, nothing more.

Both running backends share one harness (same fixtures, same scorer, both on
GPU). The #161 "pyannote vs alternatif" requirement is **met** in that both are
wired and measurable. NeMo/cloud are compared on license/VRAM/KVKK only.
Evidence: `docs/pr-diar-01-line-161-matrix-report.md`,
`docs/evidence/diar-results-2026-06-17.jsonl`.

## Speaker → person mapping

`app/services/speaker_mapping.py` (no biometrics):
- `summarize_speakers` — contextual facts per anonymous label (talk time, turns,
  first-seen);
- `apply_mapping` — overlays a **human-confirmed** `{SPEAKER_00: "Ayşe"}` mapping
  while keeping the anonymous label canonical (reversible);
- `suggest_mapping` — best-effort ordering heuristic vs a known attendee roster,
  explicitly a suggestion to be human-reviewed, never automatic identification.

> The "human-confirmed" requirement is a *code helper* today (suggest vs apply are
> separate), but the actual UI/process enforcement that a human approves a mapping
> before any name is persisted is a **separate contract**, not guaranteed by this
> module alone (review #164, Codex). To be tracked as its own issue before any
> name-linking ships.

## Decision

Only the placement and identity decisions are firm now; **backend selection is
deliberately NOT decided in this ADR** (review #164 — the synthetic smoke set
cannot rank backends).

1. **Placement (FIRM):** diarization runs as **post-processing batch**, not a
   live model (8 GB constraint). Backend-independent, settled.
2. **Backend (UNDECIDED):** pyannote 3.1 and speechbrain ECAPA are both **wired
   and measurable candidates**. No primary is chosen here. Their trade-off space
   is recorded — speechbrain costs 7× less VRAM (307 MB) and is 9× faster, which
   matters under the 8 GB budget; pyannote is a single end-to-end pipeline — but
   which one wins on Turkish DER is **open until a collar=0.25 measurement on a
   realistic fixture + pilot**. The synthetic cells do not settle even the
   relative ranking (see ¹).
3. **Identity (FIRM):** anonymous labels canonical; names only via
   human-confirmed overlay (KVKK).

## Explicit Non-Decision

- No backend is locked. A single n=1 smoke DER (45.04%) is **not** a decision-grade
  number — it reflects a synthetic, overlap-free fixture (pyannote may misestimate
  speaker count) and one sample.
- No DER target is asserted yet; it is set with the pilot (G-WER gate).

## Reopen / Promote-to-ACCEPTED Triggers

Promote only when ALL hold:
- pyannote + speechbrain harness runs (n≥5) — ✅ done (n=6, synthetic-smoke);
- collar=0.25 scoring — ✅ done (now the default; 47.8% / 54.6% on the smoke set);
- the same on a **realistic fixture** (overlap + distinct, non-identical speaker
  audio) — ⬜ pending (current cells are still synthetic-smoke);
- a real-meeting (pilot) DER calibrates absolute numbers (gated on go-live #59 /
  consent, like ADR-0031's pilot leg) — ⬜ pending;
- chosen backend meets the agreed diarization DER target (G-WER) — ⬜ pending.

## Cross-AI Consensus

Required, not yet complete: this DRAFT (Cursor Fable 5) + independent reviewer +
human/operator approval all pending.

## Consequences

Positive: candidate matrix + criteria + measurement harness are explicit; the
8 GB and KVKK constraints are recorded; an alternative is genuinely measurable,
not hand-waved.

Negative: backend choice stays provisional until the GPU sweep + alternative +
pilot land; absolute DER cannot be claimed yet.
