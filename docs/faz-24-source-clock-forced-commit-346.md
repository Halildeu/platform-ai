# Source-clock floor for forced STT commits (#346)

Tracked by #346. Blocks Product Slice Halildeu/platform-k8s-gitops#3399 and
quality acceptance Halildeu/platform-k8s-gitops#3753.

## Observed failure

GPU rollout 34791935528 rejected candidate
`5b38ff0504cb3909df384df1c89c82a503e3535f` and automatically restored
`d67ec2d591f70293ed23cdfe48726baf6a20ef12`. The changed source in that
candidate was meeting analysis delivery, not STT.

A fixed four-run diagnostic on the restored source used the same fixture,
model, smoke client and acceptance thresholds. Client pacing factors were
preselected as 1, 2, 1, 2; there was no retry-until-pass selection. WER was
0, 0.2, 0.4, 0.2 respectively. The third run forced a final after only
73,600 source samples (4.6 seconds), then emitted another final from the
retained tail. At pacing factor 2, forced windows were as short as 38,400
samples despite the configured 5-second forced budget.

The metadata-only diagnostic SHA-256 is
`c6606faac6e36289dd69686c82981b85dfe1f859e509c332b80d58086e25ab82`.
Fixture SHA-256:
`2039e3c6b5c53c4759bc93723157dadf9c44228ef1d5e7143ad0587c43417053`.
This is controlled synthetic diagnostic evidence, not human meeting quality
acceptance or internal diarization qualification.

## Narrow repair

Forced commits require both the existing wall age and at least
`ceil(forced_commit_sec * SAMPLE_RATE)` source samples. Network scheduling
alone can no longer trigger a forced commit on a shorter source window.
Silence, EOF/drain, retained-tail behavior, memory limits, partial timing,
models and configured thresholds are unchanged.

Two WebSocket regression cases first failed on the original implementation.
They wait beyond the wall budget with one sample less than the source
budget, then either send EOF or supply the last sample. They verify exact
source coordinates, a single final and the terminal acknowledgement/drain.
Four existing forced-transport fixtures now supply enough source samples
to legitimately reach the unchanged forced budget; their transport/EOF
assertions remain intact.

## Verification boundary

Local verification: 361 tests passed, 3 real-audio integration tests
deselected by the existing default configuration, 82% app coverage.
All 39 WebSocket contract tests passed. Deployment acceptance still requires
exact-SHA GPU rollout, the same fixed pacing diagnostic, and a new persisted
TEST meeting with browser semantics/date/source and allow/deny checks.
No production mutation or threshold relaxation is authorized by this note.
