# PR-wer-01 Line #37 Execution Report

Issue: `#37 [PR-wer-01] ADR girdisi: medium int8 vs large-v3-turbo`

## Purpose

Record a technically honest STT model decision while the real Workcube pilot
dataset is unavailable.

## Requirement Mapping

| #37 requirement | Result |
|---|---|
| ADR at `docs/adr/0031-stt-production-model-choice.md` | Added |
| Compare `medium int8` | Added with reproducible CPU measurements |
| Compare `large-v3-turbo` | Added as unselected candidate with pending evidence |
| Compare `medium fp16` | Added as provisional live draft role |
| Compare `large-v3` | Added as provisional final revision role |
| WER + latency + cost + RAM matrix | Added; unavailable values explicitly marked `PENDING` |
| Decision rationale | Added |
| Open issues | Added |
| No `large-v3-turbo` lock | Enforced |
| Cross-AI consensus | Status recorded as pending; ADR cannot become ACCEPTED yet |

## Provisional Decision

| Role | Model |
|---|---|
| Live draft | `medium/cuda/float16`, beam size 1 |
| Final revision | `large-v3/cuda/float16`, beam size 1 |
| CPU fallback | `medium/cpu/int8` |

## Why #35 / #36 Were Not Fabricated

#35 and #36 require a Workcube pilot dataset. The real recording needs:

- participant consent;
- operator approval;
- encrypted storage;
- manual ground truth;
- second-person review.

Those inputs do not exist yet. Therefore the ADR records the dependency and
uses only available evidence. It does not invent pilot WER or GPU RAM values.

## Workcube Recording Blocker

A real Workcube meeting recording cannot currently be performed. Therefore:

- #35 and #36 were temporarily skipped, not completed;
- no Workcube WER, CER, latency, accuracy, or model-comparison result is
  claimed;
- #37 uses only the models and observations already tested in the PoC;
- the decision remains `PROVISIONAL`;
- the project must return to #35 and #36 after an approved Workcube recording,
  participant consent, encrypted storage, manual ground truth, and review are
  available.

This deferral prevents the missing Workcube test from blocking all subsequent
engineering work, but it does not remove the test requirement.

## Evidence Used

Repository CPU baseline:

- `medium/cpu/int8`
- `5.52s` audio
- `7.701s` warm inference
- `1.503GiB` peak observed memory
- Docker smoke PASS
- integration tests `3 passed`

Approved GPU PoC observations:

- `medium/cuda/float16` used for live draft;
- `large-v3/cuda/float16` used for final revision;
- workflow approved as useful;
- canonical comparable WER/RAM/cost measurements unavailable.

## Sapma

There is one explicit process deviation:

- Normal order is #35 -> #36 -> #37.
- Workcube pilot recording cannot currently be performed.
- #37 is therefore written as `PROVISIONAL`, not `ACCEPTED`.

This deviation allows engineering to continue without hiding the missing
evidence. It does not close #35 or #36 and does not permanently lock a model.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Pilot-domain accuracy unknown | Workcube vocabulary may perform worse | ADR reopen required after #34/#35 |
| GPU RAM/cost values missing | Capacity planning incomplete | #43 performance matrix required |
| `large-v3` final may be too slow under concurrency | Final queue may grow | #42 multi-worker GPU test |
| `medium` draft may revise heavily | Poor visual stability | Measure chunk/final drift in #35 |
| Provisional decision mistaken for final | Wrong production lock | ADR status and non-decision stated repeatedly |

## Validation

Executed checks:

```bash
rg -n "PROVISIONAL|medium int8|medium fp16|large-v3-turbo|large-v3|WER|latency|cost|RAM|Cross-AI" docs/adr/0031-stt-production-model-choice.md
git diff --check
```

Results:

- required decision terms and all four model candidates were found in the ADR;
- `git diff --check` passed;
- no application code, model configuration, or runtime behavior was changed.

## Files

- `docs/adr/0031-stt-production-model-choice.md`
- `docs/pr-wer-01-line-37-execution-report.md`

AG-019 staging resource gate pending; implementation validated locally only.
