# PR-final-stt-01 Line #38 Execution Report

Issue: `#38 [PR-final-stt-01] final-stt-service skeleton (WER sonrasi model)`

Date: 2026-06-08

## Purpose

Create the first runnable `final-stt-service` compute-worker skeleton. The
service consumes 10-15 second contextual audio jobs, runs the provisional final
Whisper model, merges the revised chunk with previously committed text, and
publishes a final transcript candidate.

This is not the complete UI revision state machine. The full
`draft -> stabilizing -> final -> revised` lifecycle remains #39.

## Requirement Mapping

| #38 requirement | Implementation |
|---|---|
| `services/final-stt-service/` | Placeholder replaced with a FastAPI service |
| Reuse `live-stt-service` pattern | FastAPI, Pydantic Settings, faster-whisper, Prometheus, structlog, tests and Dockerfile |
| 10-15 second audio chunk | Configurable strict declared-duration validation; detected-duration tolerance check |
| Selected model after WER | Provisional ADR-0031 choice: `large-v3/cuda/float16`, beam size 1 |
| Segment merge + revision logic | Deterministic longest word-overlap merge |
| Redis consumer pattern | Redis Streams consumer group, result stream, dead-letter stream and ACK semantics |

## Architecture Added

```text
audio-gateway / upstream producer
        |
        | Redis Stream: stt:final:jobs
        v
final-stt-service
  - validates message and 10-15s duration
  - resolves audio under an allowed root
  - verifies pinned model hash in staging/production
  - runs large-v3 contextual transcription
  - overlap-merges committed text and final chunk
        |
        | Redis Stream: stt:final:results
        v
transcript integration / #39 state propagation
```

Audio bytes are not embedded in Redis. The input message carries a path to an
audio object exposed under `FINAL_STT_AUDIO_ROOT`. Arbitrary host filesystem
paths are rejected.

## Model Decision

The Workcube pilot recording cannot currently be performed. Therefore #35 and
#36 are temporarily skipped, not completed.

The models already tested in the approved PoC are used:

| Role | Provisional model |
|---|---|
| Live draft | `medium/cuda/float16` in `live-stt-service` |
| Final revision | `large-v3/cuda/float16` in this service |
| CPU fallback | `medium/cpu/int8` |

No `large-v3-turbo` production lock was introduced. This service must be
re-evaluated after Workcube pilot evidence and the #35/#36 matrix exist.

## Model Pinning

`faster-whisper==1.0.3` does not expose a Hugging Face revision argument through
`WhisperModel`. Merely recording a revision environment variable would not
enforce drift protection.

The service therefore requires the following in staging and production:

- `FINAL_STT_MODEL_PATH`: pre-downloaded approved model directory;
- `FINAL_STT_MODEL_REVISION`: approved source revision recorded for operations;
- `FINAL_STT_MODEL_SHA256`: expected SHA-256 of `model.bin`.

The model is loaded with `local_files_only=True`, and `model.bin` is hashed
before load. A mismatch fails closed.

## Redis Semantics

- Consumer group is created idempotently.
- A valid job is ACKed only after the result is published.
- Invalid schema/path/duration jobs go to the dead-letter stream and are ACKed.
- Unexpected inference or Redis failures remain pending for retry.
- Result and dead-letter streams use bounded approximate `MAXLEN`.
- Logs contain correlation ID, chunk sequence, elapsed time and error class.
- Logs do not contain transcript text, audio bytes, session ID or audio path.

## Files Added or Replaced

Service/runtime:

- `services/final-stt-service/app/main.py`
- `services/final-stt-service/app/core/config.py`
- `services/final-stt-service/app/models/schemas.py`
- `services/final-stt-service/app/services/transcribe.py`
- `services/final-stt-service/app/services/merge.py`
- `services/final-stt-service/app/services/consumer.py`
- `services/final-stt-service/app/api/health.py`
- `services/final-stt-service/app/api/metrics.py`

Packaging:

- `services/final-stt-service/requirements.txt`
- `services/final-stt-service/requirements-dev.txt`
- `services/final-stt-service/pyproject.toml`
- `services/final-stt-service/Dockerfile`
- `services/final-stt-service/README.md`

Tests:

- `services/final-stt-service/tests/conftest.py`
- `services/final-stt-service/tests/unit/test_config.py`
- `services/final-stt-service/tests/unit/test_consumer.py`
- `services/final-stt-service/tests/unit/test_health.py`
- `services/final-stt-service/tests/unit/test_merge.py`
- `services/final-stt-service/tests/unit/test_transcribe.py`

The placeholder `.gitkeep` files were removed.

## Validation Evidence

Executed under the isolated `services/final-stt-service/.venv`:

| Check | Result |
|---|---|
| `pytest -q -p no:cacheprovider` | `13 passed` |
| `pytest --cov=app` | `13 passed`, total coverage `89%` |
| `ruff check --no-cache app tests` | PASS |
| `mypy app` strict configuration | PASS, 13 source files |
| `black --check app tests` | PASS, 19 files unchanged |
| `pip check` | No broken requirements |
| `compileall app tests` | PASS |
| `git diff --check` | PASS |

Unit tests use fake audio/model/Redis objects. No Whisper model was downloaded,
no GPU was accessed, and no Workcube recording was used.

## Validation Not Executed

Docker image build:

- Docker CLI exists on the laptop.
- Docker Engine is not running.
- The build was not claimed as passed.
- `Dockerfile.gpu` remains #41 scope.

Real Redis integration:

- Consumer publish/ACK/dead-letter behavior is unit tested.
- No reachable staging Redis resource was provided.
- AG-019 remains pending.

Real GPU inference:

- This laptop did not run `large-v3/cuda/float16`.
- GPU PC installation and CUDA image validation remain #41/#42.

## Scope Deviations

### Provisional model instead of completed WER winner

Normal order requires #35 and #36 before #38. Workcube recording is currently
unavailable, so ADR-0031 permits the already tested `large-v3` final role on a
provisional basis. This does not close #35 or #36.

### Secure local path instead of final MinIO adapter

The issue requests a Redis consumer pattern but no active MinIO object contract
exists in this repo. The skeleton uses an allow-listed local audio root suitable
for a secure mount/download adapter. MinIO host integration is tracked
separately and must not be invented in this issue.

### Basic merge only

#38 includes the deterministic overlap merge required to produce a revised
candidate. The multi-state lifecycle, state transitions, diff contract and UI
propagation intentionally remain #39.

## Risks and Follow-up

| Risk | Impact | Required follow-up |
|---|---|---|
| Workcube WER unknown | Final model may be suboptimal for domain vocabulary | Return to #35/#36 |
| No live Redis integration | Queue behavior not proven against staging | Resolve AG-019 and run integration test |
| No GPU container validation | CUDA/cuDNN/runtime compatibility unproven | #41 |
| Single consumer/inference path | Throughput and VRAM concurrency unknown | #42 |
| Basic overlap merge | UI can still show unstable state transitions | #39 |
| Audio mount contract provisional | Cross-host object retrieval unresolved | MinIO/GitOps integration |
| Branch is based on prior sequential work | Upstream PR dependency exists | Merge/rebase prerequisite branches in order |

## Next Step

#39: implement the explicit `draft -> stabilizing -> final -> revised` state
machine, diff/overlap behavior and UI-facing state propagation contract.

AG-019 staging resource gate pending; implementation validated locally only.
