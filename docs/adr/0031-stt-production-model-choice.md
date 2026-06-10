# ADR-0031: STT Production Model Choice

- Status: PROVISIONAL - PILOT EVIDENCE PENDING
- Date: 2026-06-08
- Issue: `#37 [PR-wer-01] ADR girdisi: medium int8 vs large-v3-turbo`
- Decision scope: STT model roles before Workcube pilot WER evidence is available

## Context

The target architecture has two transcription stages:

1. live draft text must appear quickly while the user is speaking;
2. a larger model revises the draft into a more accurate final transcript.

The planned #35/#36 triangulation requires Common Voice, synthetic, and a
privacy-safe Workcube pilot meeting. Common Voice data is ready, but the real
Workcube pilot recording requires operator approval, participant consent,
encrypted storage, and manual ground truth. That operator action is not
currently available.

This ADR therefore records a provisional operational choice using only the
models already exercised in the project work. It does not claim a complete WER
decision and does not permanently lock `large-v3-turbo`.

## Evidence Available

### Repository CPU baseline

The reproducible repository baseline is:

| Field | Result |
|---|---:|
| Model | `medium` |
| Device | CPU |
| Compute | `int8` |
| Audio duration | `5.52s` |
| Warm transcription | `7.701s` |
| Cold API elapsed | `10.872s` |
| Approx. model load + overhead | `29.226s` |
| Peak observed memory | `1.503GiB` |
| Integration result | `3 passed` |
| Docker smoke | PASS |

Source: `docs/poc-stt-baseline.md`.

Interpretation: `medium/int8/cpu` is a valid fallback and regression baseline,
but it is too slow for an iPhone-like live dictation experience.

### Approved GPU PoC observations

The separately approved GPU PoC exercised this two-model pattern:

- live draft: `medium`, CUDA, `float16`, beam size 1;
- final revision: `large-v3`, CUDA, `float16`, beam size 1.

Observed engineering result:

- GPU inference was materially faster than the CPU baseline;
- draft text could be displayed during speech;
- the larger final model could revise committed text;
- the workflow was approved as a useful PoC;
- exact comparable WER, CER, peak RSS, and cost measurements were not persisted
  in the canonical repository.

These observations are operational evidence, not a complete #35 metric matrix.

## Candidate Matrix

`PENDING` means the metric was not measured under the same reproducible dataset
and hardware protocol.

| Candidate | Intended role | Device/compute | WER/CER | Latency | RAM | Cost | Evidence |
|---|---|---|---|---|---|---|---|
| `medium int8` | CPU fallback | CPU / int8 | PENDING full matrix | `7.701s` warm for `5.52s` audio | `1.503GiB` peak observed | No GPU cost | Reproducible repo baseline |
| `medium fp16` | Live draft | GPU / float16 | PENDING | Fast enough for approved draft PoC; exact matrix pending | PENDING | Existing GPU host | Approved GPU PoC |
| `large-v3` | Final revision | GPU / float16 | PENDING | Suitable for asynchronous final pass in PoC; exact matrix pending | PENDING | Existing GPU host | Approved GPU PoC |
| `large-v3-turbo` | Candidate draft/final alternative | GPU / float16 or int8 | PENDING | PENDING reproducible matrix | PENDING | Existing GPU host | Not locked; insufficient canonical evidence |

## Decision

Until the Workcube pilot dataset and #35/#36 measurements are available:

| Role | Provisional model |
|---|---|
| Live draft | `medium`, CUDA, `float16`, beam size `1` |
| Final revision | `large-v3`, CUDA, `float16`, beam size `1` |
| CPU fallback / Docker regression | `medium`, CPU, `int8` |

This is a role-based choice. One model is not forced to satisfy both live
latency and final accuracy.

## Why This Decision

1. It uses models already exercised in the project instead of introducing a
   new unmeasured production assumption.
2. `medium/int8/cpu` has reproducible repository evidence but misses the live
   latency target.
3. `medium/float16/gpu` was fast enough to serve as the live draft layer in the
   approved PoC.
4. `large-v3/float16/gpu` is reserved for the slower final correction layer,
   where accuracy is more important than immediate display.
5. `large-v3-turbo` remains a candidate. It is not selected merely because its
   name suggests speed.

## Explicit Non-Decision

`large-v3-turbo` is not locked as the production model.

The ADR must be reopened before any permanent model lock. The following
evidence is still required:

- Common Voice WER/CER across all candidates;
- synthetic pipeline smoke;
- Workcube pilot WER/CER after explicit consent;
- Turkish normalization error counts;
- special name/company/product recognition;
- latency per audio minute;
- peak GPU/CPU memory;
- timeout rate;
- chunk-to-final transcript drift;
- operating cost comparison.

## #35 / #36 Deferral

#35 and #36 are not declared complete.

Reason:

- a real Workcube meeting recording cannot currently be made;
- #34 real Workcube pilot recording is operator-action;
- participant consent is not yet collected;
- encrypted storage target is not yet confirmed;
- manual pilot ground truth does not exist.

For this reason, #35 and #36 were temporarily skipped so work could continue
with the models already tested in the PoC. This is not a completion or
acceptance claim. The project must return to #35 and #36 when an approved
Workcube recording and reviewed ground truth become available.

The Common Voice dataset prepared by #33 remains available. The pilot rows in
the future #35 matrix must remain `PENDING` until operator evidence arrives.

## Reopen Triggers

Reopen this ADR when any of the following happens:

- #34 produces an approved pilot recording and reviewed ground truth;
- #35 completes the 8-metric matrix;
- #36 completes three-dataset triangulation;
- GPU hardware changes;
- a tested model fails the live latency or accuracy acceptance threshold;
- `large-v3-turbo` gains reproducible evidence better than the selected role
  model.

## Open Issues

- #34: operator must complete real pilot recording.
- #35: WER + 8 metric matrix remains incomplete.
- #36: three-dataset triangulation remains incomplete.
- #41/#42: canonical GPU Docker and multi-worker implementation are not yet
  integrated.
- #43: post-GPU performance matrix is still required.
- AG-019 staging resource gate remains pending.

## Cross-AI Consensus

Required by #37, but not yet completed.

Current status:

- Codex implementation/review: this ADR draft;
- human/operator approval: pending;
- independent AI reviewer: pending.

This document must not be promoted from `PROVISIONAL` to `ACCEPTED` until the
required independent review and missing metric evidence are recorded.

## Consequences

Positive:

- development can continue without pretending the Workcube pilot exists;
- the approved two-stage GPU PoC is preserved;
- CPU fallback remains reproducible;
- future model comparison has explicit reopen criteria.

Negative:

- production model choice remains provisional;
- exact WER/CER and GPU memory/cost claims cannot yet be made;
- #35 and #36 remain open dependencies.
