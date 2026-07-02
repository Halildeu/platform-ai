# ADR-0033: Diarization approach

- Status: **PROPOSED - OWNER REVIEW**
- Date: 2026-06-17
- Decision evidence updated: 2026-07-02
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
| pyannote 3.1 | **17.88%** | 0.026 | 1743 ms | 2154 MB | Passes DER <= 30% |
| SpeechBrain ECAPA | 20.95% | **0.006** | **332 ms** | **366 MB** | Passes DER <= 30% |

### Controlled real-voice overlap set

The set contains three two-speaker fixtures built from consented, distinct
speaker turns with deterministic overlap. Total evaluated audio is 81 seconds.

| Backend | Corpus DER | Mean DER | Max DER | RTF | p50 | Peak VRAM delta |
|---|---:|---:|---:|---:|---:|---:|
| pyannote 3.1 | **20.30%** | **19.20%** | 31.31% | 0.025 | 635 ms | 2235 MB |
| SpeechBrain ECAPA | 32.15% | 32.64% | 35.53% | **0.006** | **103 ms** | **411 MB** |

SpeechBrain is faster and lighter, but its overlap corpus DER exceeds the
agreed 30% quality ceiling. Pyannote stays below that ceiling in both the pilot
and overlap evaluations.

## Decision

1. **Placement:** run diarization as a post-processing batch step. It is not a
   third continuously resident live model on the 8 GB GPU.
2. **Primary backend:** use self-hosted
   `pyannote/speaker-diarization-3.1` on CUDA. Accuracy is the primary product
   criterion for #161, and pyannote is the only measured candidate that passes
   the DER ceiling on both pilot and overlap evidence.
3. **Fallback:** retain SpeechBrain ECAPA as an explicit resource-constrained
   degraded-mode candidate. It is not the primary backend because its overlap
   DER is 32.15%.
4. **Identity boundary:** keep anonymous speaker labels canonical. Human
   confirmation is required before applying a person label. This ADR does not
   enable embeddings, voiceprints, or automatic biometric identification.
5. **Scheduling boundary:** do not co-load pyannote with the full STT and Ollama
   model set without an explicit GPU capacity check. The measured pyannote VRAM
   delta is about 2.2 GB.

The existing `speaker_mapping.py` helpers remain within that boundary:
`summarize_speakers` reports anonymous talk-time/turn facts, `suggest_mapping`
is advisory only, and `apply_mapping` is reserved for a human-confirmed
overlay. Anonymous labels remain the canonical reversible representation.

## Decision gate

The metadata-only selected-backend row is:

`docs/evidence/diar-decision-pilot-2026-07-02.jsonl`

The metadata-only overlap comparison is:

`docs/evidence/diar-overlap-results-2026-07-02.jsonl`

It is evaluated with:

```powershell
python services/diarization-service/scripts/diar_decision_gate.py `
  --evidence docs/evidence/diar-decision-pilot-2026-07-02.jsonl `
  --max-der 0.30 `
  --max-rtf 0.05 `
  --max-latency-ms 3000 `
  --max-peak-vram-delta-mb 2500 `
  --min-samples 3
```

Expected result: `status=pass`, `findingCount=0`, selected backend `pyannote`.

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
- The measured rows used the cached model snapshot with `revision=null`.
  Production packaging must pin the resolved model revision/hash under the
  repository model-versioning rule; this does not change the measured backend
  choice.

## Acceptance

The prior promotion triggers are now satisfied:

- pyannote and SpeechBrain use the same GPU measurement harness;
- `collar=0.25`, `skip_overlap=false`;
- real pilot DER exists for both candidates;
- a distinct real-voice overlap set exists for both candidates;
- pyannote meets the accepted 30% corpus DER ceiling on both sets;
- the canonical G-WER/DER gate passed with WER 6.47% and pyannote DER 17.88%.

Owner review is the final step before changing this ADR status from PROPOSED to
ACCEPTED and closing #161.
