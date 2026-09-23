# Incremental live analysis

The phone acceptance requires decisions and tasks to update as speech progresses.
A saved result minutes after recording, or a single partial summary before EOF,
does not satisfy that requirement.

`/analyze/live` uses a compact extractive prompt. A content-free cursor carries the
redacted prefix length/hash and up to 23 grounded sentence indices. The next call
reconsiders those active claims, the last three preceding sentences and every new
sentence. Cancellation, completion and reassignment must remove superseded work.
The model must select each claim again; previous output is never blindly copied.
All citations and owner/date checks still use the full redacted source.
Historical summary citations are not carried as active claims: a summary may
mention cancelled work. Live summaries therefore use the active claims and recent
speech; the complete meeting summary is still produced by final analysis.

Invalid hints, transcript corrections and rolling-window truncation cause a full
source fallback. The gateway keeps the hint only in its existing meeting-local
aggregation. There is no extra transcript cache. Final analysis ignores the hint
and uses its existing canonical transcript and qualified prompt.

## Verification and rollout

Unit tests prove transport, selection materialization, cursor invalidation, PII
guards and original citation offsets. Mocked model answers are not semantic or
latency acceptance. Reduced context can omit older unselected context; verify
recall and cancellation/reassignment with the real qualified model before rollout.

Run from the service directory on an authorized test runner with the existing
qualified model environment and `PYTHONPATH=.`:

```text
python scripts/live_incremental_probe.py --max-update-seconds 5
```

The probe makes five synthetic live updates and requires the expected active task
sets with grounded owners/dates and the adopted policy decision. Explicit task
cancellation decisions are optional. A per-call five-second model budget applies,
including the first call. Output
contains only metadata. A failure remains a failure; do not loosen the budget to
claim acceptance. This is a proposed engineering gate, not a measured guarantee.
It does not measure transport, speech recognition or phone rendering.

Backend rollout additionally needs sentence-trigger mode and a shorter cadence in
the TEST overlay. Keep single-flight/latest-snapshot coalescing. Verify cue-sentence
completion to phone update, including a second decision, task reassignment and
cancellation while recording stays open. Do not count a cached V1 summary or a
durable result as live acceptance. Roll back exact source/config if this fails.

If the qualified model cannot meet the live budget, evaluate a separately qualified
fast live model alongside the existing final-analysis model instead of hiding the
delay with a longer wait. No model replacement is made by this change.
