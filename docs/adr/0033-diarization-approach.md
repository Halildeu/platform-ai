# ADR-0033: Diarization approach

- Status: **ACCEPTED**
- Date: 2026-06-17
- Decision evidence updated: 2026-07-03 (revision-resolved re-measurement, #235)
- Accepted: 2026-07-16 (owner re-review and source acceptance)
- Issue: `#161 [Faz24 T-B] STT quality evidence - Turkish WER + diarization`
- Amended by: ADR-0035 (voiceprint remains legal-gated)

## Context

Faz 24 needs measured Turkish speaker diarization, not a model choice based on
reputation. The decision must also respect two product constraints:

1. The GPU host is an RTX 4070 with 8 GB VRAM. Diarization must not compete
   continuously with live STT, final STT, and Ollama.
2. Diarization output is anonymous `SPEAKER_xx` by default. Automatic identity
   or voiceprint processing remains outside this decision and subject to the
   ADR-0035 legal gate.

The original synthetic, overlap-free measurements were useful only for proving
the harness. They were not used to select a backend. The selection below uses
consented pilot speech plus a controlled real-voice overlap set, scored with
`collar=0.25` and `skip_overlap=false`.

## Measured candidates

All values are metadata-only. No audio, transcript, RTTM, participant name, or
speaker identity is stored in this repository.

### Consented pilot set

| Backend | Corpus DER | RTF | p50 | Peak VRAM delta | Result |
|---|---:|---:|---:|---:|---|
| pyannote 3.1 | **17.88%** | 0.026 | 1739 ms | 2154 MB | Passes DER <= 30% |
| SpeechBrain ECAPA | 23.14% | **0.005** | **292 ms** | **367 MB** | Passes DER <= 30% |

Backed by `docs/evidence/diar-pilot-comparison-2026-07-03.jsonl` (both rows,
real `resolved_revision` — #235 re-review P1: the prior table cited a
SpeechBrain pilot number with no committed evidence file behind it).

### Controlled real-voice overlap set

The set contains three two-speaker fixtures built from consented, distinct
speaker turns with deterministic overlap. Total evaluated audio is 81 seconds.

| Backend | Corpus DER | Mean DER | Max DER | RTF | p50 | Peak VRAM delta |
|---|---:|---:|---:|---:|---:|---:|
| pyannote 3.1 | **20.30%** | **19.20%** | 31.31% | 0.024 | 634 ms | 2234 MB |
| SpeechBrain ECAPA | 33.14% | 33.80% | 37.57% | **0.006** | **107 ms** | **410 MB** |

Backed by `docs/evidence/diar-overlap-results-2026-07-03.jsonl` (both rows,
real `resolved_revision`); figures updated from an earlier informal run to
match this committed evidence exactly.

SpeechBrain is faster and lighter, but its overlap corpus DER exceeds the
agreed 30% quality ceiling. Pyannote stays below that ceiling in both the pilot
and overlap evaluations.

**Ceiling scope (Codex review #235):** the accepted 30% ceiling is a
**corpus-level** ceiling (`der_corpus`, duration-weighted across all fixtures
in the set) — not a per-fixture ceiling. Pyannote's overlap `der_max` (the
single worst fixture) is 31.31%, above 30%; its `der_corpus` (20.30%) is what
the gate scores and what this decision is conditioned on. A per-fixture
ceiling is not currently enforced by `diar_decision_gate.py` and is not
proposed here — if one is wanted later, the gate needs an explicit
`max-der-max`-style threshold added, not an implicit reading of this table.

## Decision

1. **Placement:** run diarization as a post-processing batch step. It is not a
   third continuously resident live model on the 8 GB GPU.
2. **Primary backend:** use self-hosted
   `pyannote/speaker-diarization-3.1` on CUDA. Accuracy is the primary product
   criterion for #161, and pyannote is the only measured candidate that passes
   the DER ceiling on both pilot and overlap evidence.
3. **Fallback:** retain SpeechBrain ECAPA as an explicit resource-constrained
   degraded-mode candidate. It is not the primary backend because its overlap
   DER is 33.14% (see overlap table above).
4. **Identity boundary:** keep anonymous speaker labels canonical. Human
   confirmation is required before applying a person label. This ADR does not
   enable embeddings, voiceprints, or automatic biometric identification —
   voiceprint/biometric processing (KVKK m.6, special-category data) stays
   gated behind ADR-0035's legal track (#168) regardless of this decision.
5. **Scheduling boundary:** do not co-load pyannote with the full STT and Ollama
   model set without an explicit GPU capacity check. The measured pyannote VRAM
   delta is about 2.2 GB.

The existing `speaker_mapping.py` helpers remain within that boundary:
`summarize_speakers` reports anonymous talk-time/turn facts, `suggest_mapping`
is advisory only, and `apply_mapping` is reserved for a human-confirmed
overlay. Anonymous labels remain the canonical reversible representation.

## Decision gate

The metadata-only selected-backend row is:

`docs/evidence/diar-decision-pilot-2026-07-03.jsonl`

The metadata-only overlap comparison is:

`docs/evidence/diar-overlap-results-2026-07-03.jsonl`

Both carry a real `resolved_revision` (the HF commit hash actually backing the
cached model at measurement time), addressing Halil's #235 finding that the
prior 2026-07-02 rows had `revision=null` with no gate enforcement. The
superseded 2026-07-02 files remain in git history for audit but are no longer
the evidence this gate is evaluated against.

It is evaluated with:

```powershell
python services/diarization-service/scripts/diar_decision_gate.py `
  --evidence docs/evidence/diar-decision-pilot-2026-07-03.jsonl `
  --max-der 0.30 `
  --max-rtf 0.05 `
  --max-latency-ms 3000 `
  --max-peak-vram-delta-mb 2500 `
  --min-samples 3
```

Linux/macOS (bash) equivalent:

```bash
python services/diarization-service/scripts/diar_decision_gate.py \
  --evidence docs/evidence/diar-decision-pilot-2026-07-03.jsonl \
  --max-der 0.30 \
  --max-rtf 0.05 \
  --max-latency-ms 3000 \
  --max-peak-vram-delta-mb 2500 \
  --min-samples 3
```

Expected result: `status=pass`, `findingCount=0`, selected backend `pyannote`.

**Pilot "both candidates" acceptance (#235 re-review):** the pilot table's
SpeechBrain row above is backed by a committed row with real
`resolved_revision`, not prose alone:

```bash
python services/diarization-service/scripts/diar_decision_gate.py \
  --evidence docs/evidence/diar-pilot-comparison-2026-07-03.jsonl \
  --backend pyannote \
  --max-der 0.30 \
  --max-rtf 0.05 \
  --max-latency-ms 3000 \
  --max-peak-vram-delta-mb 2500 \
  --min-samples 3
```

Expected result: `status=pass`, `findingCount=0`, selected backend `pyannote`.
(`diar-pilot-comparison-2026-07-03.jsonl` is the two-backend comparison set;
`diar-decision-pilot-2026-07-03.jsonl` above remains the single accepted-row
file the go-live decision is conditioned on — the `--backend pyannote` filter
is what makes the two consistent even though the comparison file's unfiltered
selection also happens to pick SpeechBrain here, same masking risk as
overlap, harmlessly, since SpeechBrain's own pilot DER is also under 30%.)

**Overlap acceptance (#235 re-review):** the "pyannote overlap corpus DER <=
30%" claim above is machine-verified with `--backend pyannote`, not left as
prose. Without a backend filter, the gate's cross-backend selection picks
whichever row has the best combined DER+speed+VRAM score — SpeechBrain's
speed/VRAM advantage lets it "win" that score despite failing the DER
ceiling, which would report the wrong backend's failure. The filter isolates
the actual accepted-backend claim:

```bash
python services/diarization-service/scripts/diar_decision_gate.py \
  --evidence docs/evidence/diar-overlap-results-2026-07-03.jsonl \
  --backend pyannote \
  --max-der 0.30 \
  --max-rtf 0.05 \
  --max-latency-ms 3000 \
  --max-peak-vram-delta-mb 2500 \
  --min-samples 3
```

Expected result: `status=pass`, `findingCount=0`, selected backend `pyannote`.
Both commands now run in CI (`.github/workflows/ci.yml`, `repo-gates` job) so
a future evidence or threshold regression fails the build instead of relying
on this document staying accurate.

This gate covers only the source-side #161 backend decision. It does not enable
production, direct STT, voiceprint, biometric identity, or legal approval.

## Consequences

Positive:

- The primary backend is selected from measured Turkish pilot and overlap data.
- The quality ceiling, GPU cost, and privacy posture are explicit.
- SpeechBrain remains available as a measured fallback rather than being
  discarded.

Negative:

- Pyannote uses more VRAM and is slower than SpeechBrain.
- Its gated Hugging Face model requires controlled token provisioning.
- The measured rows now carry a real `resolved_revision` captured from the
  local cache at measurement time, but no explicit `--revision` was pinned
  during the run itself. Production packaging must still pin the model
  revision/hash under the repository model-versioning rule; this does not
  change the measured backend choice.

## Acceptance

The prior promotion triggers are now satisfied:

- pyannote and SpeechBrain use the same GPU measurement harness;
- `collar=0.25`, `skip_overlap=false`;
- real pilot DER exists for both candidates, committed with real
  `resolved_revision` in `docs/evidence/diar-pilot-comparison-2026-07-03.jsonl`
  (#235 re-review: previously only asserted in prose);
- a distinct real-voice overlap set exists for both candidates, committed in
  `docs/evidence/diar-overlap-results-2026-07-03.jsonl`;
- pyannote meets the accepted 30% corpus DER ceiling on both sets, and this is
  machine-gated (`--backend pyannote`) in CI, not just documented;
- the canonical G-WER/DER gate passed with WER 6.47% and pyannote DER 17.88%.

Owner re-review approved PR #235 at head `45863413` after every blocking
evidence, CI, and revision-resolution finding was addressed. This ADR therefore
accepts pyannote 3.1 as the measured primary post-processing backend and
SpeechBrain as the resource-constrained fallback for the source-side #161
quality decision.

This acceptance does not enable production, direct STT, voiceprint or biometric
identity, or legal approval. Production packaging must still pin the resolved
model revision/hash, and runtime rollout keeps its own GitOps and live-evidence
gates.

## Cross-AI Consensus

Halildeu (Cross-AI review, PR #235, 2026-07-03): CHANGES_REQUESTED — evidence
model revision was null and the gate did not enforce it (both addressed in
this revision via `diar_matrix.py`'s `resolved_revision` capture and
`diar_decision_gate.py`'s revision-or-resolved-revision requirement), plus the
corpus-vs-per-fixture DER ceiling wording above.

Update (2026-07-03): fresh pilot + overlap measurement completed on the real
GPU host with the fixed `diar_matrix.py`. `resolved_revision` now populates
for both backends (pyannote resolved via the `~/.cache/torch/pyannote`
fallback added for this fix). `diar_decision_gate.py` re-run against
`docs/evidence/diar-decision-pilot-2026-07-03.jsonl` returns `status=pass`,
`findingCount=0`, selected backend `pyannote`. Halil's subsequent re-review
approved the revised head.

Re-review (2026-07-03, Codex cross-AI, thread 019f2877): REVISE — 3 new
blocking findings + 1 non-blocking:

1. CI's diarization step only re-checked that the old synthetic snapshot
   stays blocked; it never gated the accepted evidence. Fixed: two new
   `repo-gates` CI steps run the gate against the accepted pilot and overlap
   evidence and require `status=pass`/`backend=pyannote`.
2. The "pyannote overlap corpus DER <= 30%" claim was prose-only; unfiltered
   cross-backend selection can mask a DER failure behind a faster backend's
   better combined score. Fixed: `diar_decision_gate.py` gained an optional
   `--backend` filter, used by both the ADR command and the new CI step.
3. "Real pilot DER exists for both candidates" had no committed SpeechBrain
   pilot evidence backing it. Fixed: re-measured on the GPU host and
   committed `docs/evidence/diar-pilot-comparison-2026-07-03.jsonl` (both
   backends, real `resolved_revision`); the pilot table above now matches
   this file exactly.
4. (non-blocking) `resolved_model_revision()` could return the wrong cached
   revision's hash when an explicit `--revision` was requested but a
   different revision (e.g. "main") sorted first. Fixed: it now accepts the
   requested revision and prefers an exact match; doesn't change today's
   evidence (`revision` is null on all committed rows).

All four were addressed in commit `7995e8a` (code/CI) plus the
pilot-comparison evidence commit above.

Owner re-review (2026-07-07, PR #235, head `45863413`): **APPROVE**. The
cross-root requested-revision lookup was verified, the two-root regression test
was present, all five CI jobs passed, and the PR was mergeable. PR #235 then
merged as `8abc74dfaa0ecad49ea69593299a3a766879010a`.

Owner acceptance (2026-07-16): **ACCEPTED** for the source-side #161
G-WER/DER and diarization backend decision. The production model pin, runtime
deployment, direct-STT, voiceprint/biometric processing, and legal approval
remain separate gates and are not implied by this status transition.
