# PR-final-stt-01 Line #39 Execution Report

Issue: `#39 [PR-final-stt-01] Segment merge + revize state machine`

Canonical source: live GitHub issue body read on 2026-06-08.

## Objective

Implement the complete final transcript revision lifecycle required by issue
#39:

```text
draft -> stabilizing -> final -> revised
```

The final STT 10-15 second contextual pass must revise the live draft, expose
word-level diff/overlap information, and propagate ordered state events for a
UI/downstream consumer.

## Issue Requirement Mapping

| Live issue requirement | Implementation |
|---|---|
| `draft -> stabilizing -> final -> revised` | Strict `TranscriptRevisionStateMachine` transition graph |
| Final STT 10-15 second pass revises draft | Existing #38 `FinalTranscriber` remains the final inference source |
| Diff/overlap detection | Word-indexed diff operations plus longest suffix/prefix overlap count |
| UI state propagation contract | Ordered Redis result-stream event envelope |

## Architecture

```text
Redis final STT job
        |
        v
draft event (sequence 0)
        |
        v
stabilizing event (sequence 1)
        |
        v
FinalTranscriber: 10-15 second contextual inference
        |
        v
final event (sequence 2)
        |
        v
committed + final chunk overlap merge
        |
        v
revised event (sequence 3, terminal)
        |
        v
source Redis message ACK
```

If inference or result publication fails before the terminal event, the source
message is not ACKed and remains pending for retry.

## State Contract

| Sequence | State | Text meaning | Terminal |
|---:|---|---|---|
| 0 | `draft` | Live STT draft accepted for revision | false |
| 1 | `stabilizing` | Final contextual pass is running | false |
| 2 | `final` | Final model output for the current chunk | false |
| 3 | `revised` | Committed transcript merged with corrected chunk | true |

The full `FinalSttResult` is attached only to the terminal `revised` event.
This prevents `revisedText` from leaking through the earlier `final` event.

Invalid transitions fail closed. Examples:

- initial -> stabilizing: rejected
- draft -> draft: rejected
- stabilizing -> revised: rejected

## Retry and Deduplication

Redis Streams can redeliver pending jobs. State events therefore contain:

```text
revisionId = SHA-256(sessionId + chunkSeq + correlationId)
stateSequence = 0..3
```

The same job produces the same `revisionId` on retry. UI and downstream
consumers can use `(revisionId, stateSequence)` as the idempotency key.

The hash is carried in the internal event and logs. Raw session ID, transcript,
audio bytes and audio path are not logged.

## Diff Contract

Word-level diff uses deterministic `difflib.SequenceMatcher` output with
`autojunk=false`. Tokens are compared case-insensitively with surrounding
punctuation removed, while the original display text is preserved.

Supported operations:

- `equal`
- `insert`
- `delete`
- `replace`

Every operation carries before/after word indexes and before/after display
text. This lets a UI render corrections without deriving its own ambiguous
diff.

## Overlap Contract

The existing longest suffix/prefix word-overlap algorithm is retained and
expanded with repeated-word and no-overlap tests.

Example:

```text
committed: evet karar evet karar
chunk:     evet karar tamamlandı
overlap:   2 words
revised:   evet karar evet karar tamamlandı
```

The `revised` event exposes the final `overlapWords` count.

## Redis Publication Semantics

All four events are written to the configured `stt:final:results` stream.
The source job is ACKed only after the terminal `revised` event is published.

Expected event order:

```text
draft, stabilizing, final, revised
```

A schema/path/duration validation failure still goes to the dead-letter stream
and is ACKed. Unexpected inference or Redis publication failures remain
pending for retry.

## Files Added

| File | Purpose |
|---|---|
| `app/services/diff.py` | Word-level diff model and algorithm |
| `app/services/state_machine.py` | Strict transition graph and UI event creation |
| `tests/unit/test_diff.py` | Diff operation tests |
| `tests/unit/test_state_machine.py` | Ordering, terminal, dedup and invalid transition tests |

## Files Updated

| File | Change |
|---|---|
| `app/models/schemas.py` | Diff and revision event Pydantic contracts |
| `app/services/consumer.py` | Ordered state publication and terminal ACK |
| `app/api/metrics.py` | Bounded state event counter |
| `tests/unit/test_consumer.py` | Four-event order and publish-failure retry behavior |
| `tests/unit/test_merge.py` | Repeated-word and no-overlap boundaries |
| `tests/integration/test_redis_consumer.py` | Real Redis four-event contract |
| `README.md` | State meanings, JSON contract and dedup rule |

## Scope Boundaries

### No frontend repository change

Issue #39 is in `platform-ai`. It requires a UI propagation contract, not a
platform-web implementation. This change publishes the complete event
contract. The consuming MFE must be changed in its own repository and PR.

### No model decision

The #38 provisional model configuration is not promoted to a production model
decision. Workcube WER evidence in #35/#36 remains incomplete.

### No GPU image change

The state machine is independent of runtime device. CUDA Docker packaging
remains #41 scope.

### No staging deployment

AG-019 remains open. Implementation is validated locally; staging Redis,
cross-host audio retrieval and UI integration are not claimed.

## Risks

| Risk | Impact | Control / follow-up |
|---|---|---|
| Retry duplicates pre-terminal events | UI may display an event twice | Deduplicate with `(revisionId, stateSequence)` |
| Four records per job increase stream volume | Redis retention fills faster | Existing bounded `MAXLEN`; monitor in staging |
| Word-level diff does not model character edits | Fine-grained animation is limited | Contract intentionally uses stable word indexes |
| UI consumer not yet implemented | State events are not visible in product UI | Separate platform-web integration |
| Basic overlap may be insufficient across distant context | Duplicate/missing text at complex boundaries | Expand with real pilot cases after #35/#36 |
| Staging topology unavailable | Cross-host behavior unproven | Resolve AG-019 before merge/deploy |

## Validation Evidence

| Check | Result |
|---|---|
| Unit test suite | `24 passed`, `2 deselected` integration tests |
| Coverage | `92%` total, 423 statements |
| State machine coverage | `98%` |
| Diff coverage | `100%` |
| Merge coverage | `100%` |
| Ruff | PASS |
| Mypy strict | PASS, 15 source files |
| Black check | PASS, 25 files unchanged |
| Pip dependency check | No broken requirements |
| Compileall | PASS with isolated pycache directory |
| Git diff check | PASS |
| Real Redis Streams integration | `2 passed` |
| Docker image build | PASS: `final-stt-service:issue-39` |
| Container health | HTTP 200, expected `loading` before lazy model load |
| Container metrics | HTTP 200 |
| Revision metric exposed | `final_stt_revision_events_total` present |
| Container runtime user | UID/GID `10001:10001` |
| Image size | `363119408` bytes |

The real Redis test used an isolated temporary container:

```text
container: final-stt-39-redis-e2e
image: redis:7.2-alpine
host port: 16381
persistence: disabled
result: 2 passed
```

It verified:

1. real `XADD -> XREADGROUP -> four ordered state events -> XACK`;
2. invalid job `-> dead-letter -> XACK`;
3. pending count returns to zero.

The existing `teas-redis` container and all other `teas-*` containers were
listed for collision awareness but were not modified, restarted or used.

The Docker smoke used temporary container `final-stt-39-smoke` on host port
`18212`. Both #39 temporary containers were removed after validation. The
local `final-stt-service:issue-39` image remains available.

No Workcube recording is used by these tests.

## Review Findings Resolved

The implementation review found that attaching the complete
`FinalSttResult` to the non-terminal `final` event would expose
`revisedText` before the `revised` transition. The contract was changed so
the complete result exists only on the terminal `revised` event. Tests were
rerun after this correction.

Provider-independent Cross-AI peer review is still required by repository
policy before an upstream merge. This report does not claim that external
review has occurred.

## Next Step

After #39 code and contract validation, the next open issue in the canonical
list is #40, the operator-owned hardware decision. Implementation work should
not invent that decision. #41 GPU Docker packaging depends on the selected
hardware/runtime direction.

AG-019 staging resource gate pending; implementation validated locally only.
