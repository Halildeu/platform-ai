# PR-stt-03 Line 22 Execution Report

Date: 2026-06-05

Repo: `C:\Users\zeynep.akkilic\Desktop\platform-ai-work`

Branch: `feature/pr-stt-03-timeout-kill-test`

GitHub Project item: `#22 [PR-stt-03] Test: timeout -> kill -> metric -> controlled failure`

## AG-019 Status

```text
AG-019 staging resource gate pending; implementation validated locally only.
```

#19 is still pending because this PC still does not have confirmed staging
resource access/context. This #22 work validates timeout-kill behavior locally,
in unit tests, integration tests, and Docker smoke. It is not a staging deploy
approval.

## Kisa Karar

#22 is complete locally.

This line did not add a new runtime feature. It hardened the proof around #21:

```text
timeout -> worker terminate/kill -> worker respawn -> metric increment -> HTTP 504
```

The goal was to prove that #21's hard-timeout implementation behaves as a
controlled service failure instead of leaking a stuck worker.

## Plan #22 Ne Istiyordu?

Plan line:

```text
[PR-stt-03] Test: timeout -> kill -> metric -> controlled failure
```

Expected evidence:

1. Timeout path is exercised.
2. Worker kill semantic is verified.
3. Replacement worker respawn is verified.
4. Metric `stt_worker_killed_total{reason="timeout"}` increments.
5. API returns controlled `504` instead of an unhandled exception.

## Yapilan Is

### 1. Worker kill/respawn sequence test edildi

Changed file:

```text
services/live-stt-service/tests/unit/test_worker_pool.py
```

Added test:

```text
test_worker_slot_timeout_terminate_kill_respawn_sequence
```

This test constructs a fake `_WorkerSlot` and fake process. It verifies the
hard-timeout sequence:

```text
process.terminate()
process.join(grace_sec)
process.kill()
process.join(grace_sec)
queue reset
model_loaded = False
start()
```

This directly proves the "kill + respawn" part of #22 without loading a real
Whisper model or spawning a slow subprocess in the unit test.

### 2. API 504 + metric increment test edildi

Changed file:

```text
services/live-stt-service/tests/unit/test_api.py
```

Added helper:

```text
_prometheus_metric_value(...)
```

Added test:

```text
test_worker_timeout_returns_504_and_increments_kill_metric
```

This test captures Prometheus metric values before and after a simulated
`WorkerTimeoutError` and verifies:

```text
HTTP status == 504
response detail contains "timeout"
stt_timeout_total{model="tiny"} increments by 1
stt_worker_killed_total{reason="timeout"} increments by 1
```

### 3. Existing test lint cleanup yapildi

`tests/unit/test_api.py` already had older lint issues that became visible when
targeted ruff was run on the whole file. The cleanup was test-only and did not
change runtime behavior:

- removed unused local imports
- wrapped long test strings
- added one `# noqa: RUF001` on the existing Turkish expected transcript string

## Plan Uyumu

| Plan evidence | Durum | Kanit |
| --- | --- | --- |
| Timeout path exercised | DONE | API test raises `WorkerTimeoutError` |
| Worker kill semantic verified | DONE | sequence test asserts `terminate`, `join`, `kill`, `join` |
| Worker respawn verified | DONE | sequence test asserts `start()` called |
| Metric increment verified | DONE | API test checks `stt_worker_killed_total{reason="timeout"}` |
| Controlled failure verified | DONE | API test checks HTTP `504` and timeout detail |

## Plandan Sapma Var Mi?

Kritik sapma yok.

Controlled decisions:

| Konu | Plan metni | Yapilan | Etki |
| --- | --- | --- | --- |
| Runtime feature | Test line | No production code changed | Uyumlu |
| Worker proof | timeout -> kill | Fake process sequence test | Fast, deterministic unit proof |
| Metric proof | metric increment | Prometheus text parsed before/after request | Direct contract proof |
| GPU integration | Not in #22 | Yapilmadi | Uyumlu; later integration item |

## GPU / Approved Live STT Durumu

GPU integration was not done in #22.

Reason:

```text
#22 is a test-hardening line for timeout/kill/metric/504 behavior.
```

The approved GPU live STT PoC is still planned as a later integration step.
Current assumption from Zeynep:

```text
GPU execution target = other GPU PC
Laptop/backend side = gateway/orchestration/frontend
```

Likely future integration shape:

```text
audio-gateway-service
  -> queue / routing
  -> live-stt-service on external GPU PC
```

This work prepares the service so GPU workers can fail safely later, but it
does not move the approved GPU streaming PoC into the repo yet.

## Dosya Dosya Ne Degisti?

### `tests/unit/test_worker_pool.py`

Added:

- import `MethodType`
- private `_WorkerSlot` import for direct sequence test
- `test_worker_slot_timeout_terminate_kill_respawn_sequence`

Purpose:

```text
Prove terminate -> grace join -> kill -> grace join -> queue reset -> respawn.
```

### `tests/unit/test_api.py`

Added:

- top-level `re` import for Prometheus metric parsing
- `_prometheus_metric_value(...)`
- `test_worker_timeout_returns_504_and_increments_kill_metric`

Minor cleanup:

- removed unused local `re` import
- removed unused local `uuid` import
- wrapped long xfail/test data lines
- added `# noqa: RUF001` to existing Turkish transcript assertion

Purpose:

```text
Prove timeout response is controlled and metrics increase.
```

## Test ve Dogrulama

### Targeted ruff

```bash
python -m ruff check tests/unit/test_worker_pool.py tests/unit/test_api.py
```

Result:

```text
All checks passed!
```

### Targeted unit tests

```bash
python -m pytest tests/unit/test_worker_pool.py tests/unit/test_api.py -q
```

Result:

```text
39 passed, 4 xfailed, 7 xpassed, 1 warning
```

### Full non-integration unit tests

```bash
python -m pytest -m "not integration" -q
```

Result:

```text
49 passed, 3 deselected, 4 xfailed, 7 xpassed, 1 warning
```

`xfailed/xpassed` entries are the pre-existing PII redaction tests tracked by
Issue #97. They are not introduced by #22.

### Mypy

```bash
python -m mypy app
```

Result:

```text
Success: no issues found in 13 source files
```

### Integration tests

```bash
python -m pytest -m integration -q
```

Result:

```text
3 passed, 60 deselected
```

### Docker build

First attempt failed because Docker Desktop daemon was not running:

```text
failed to connect to the docker API
```

Docker Desktop was started, daemon readiness was confirmed, and build was
retried.

Command:

```bash
docker build -t live-stt-service:dev .
```

Result:

```text
PASS
```

### Docker smoke

Command:

```bash
bash scripts/docker-smoke.sh --skip-build
```

Result:

```text
Docker smoke PASS
language: tr
duration: 5.52s
segments: 1
wall-clock: 69s
stt_transcribe_total: 1.0
```

## Riskler

| Risk | Etki | Mitigasyon |
| --- | --- | --- |
| `_WorkerSlot` private class test edildi | Internal refactor test kirabilir | #22 dogrudan worker kill semantic istedigi icin kabul edilebilir |
| Metric parsing Prometheus text'e bagli | Metric format degisirse test guncellenir | Label/value contract zaten observability yuzeyi |
| Docker smoke wall-clock 69s | Cold start/model cache yavasligi olabilir | Smoke PASS; latency optimization bu item kapsaminda degil |
| AG-019 pending | Staging kaynak etkisi bilinmiyor | Staging deploy/merge oncesi #19 kapanmali |

## Ne Yapilmadi?

This line intentionally did not:

- add GPU/CUDA code
- integrate the approved live streaming STT PoC
- change `app/services/worker.py`
- change `app/api/transcribe.py`
- change Dockerfile or deployment config
- open a PR to Halil Bey's upstream repo

## Geri Donus / Rollback Plani

If #22 test changes cause trouble:

1. Remove `test_worker_slot_timeout_terminate_kill_respawn_sequence`.
2. Remove `_prometheus_metric_value`.
3. Remove `test_worker_timeout_returns_504_and_increments_kill_metric`.
4. Keep #21 runtime code unchanged.

Rollback risk is low because this item is test-only.

## Halil Bey Reposuna Etki

Yok.

This work only affects the local/fork branch until explicitly pushed/opened as
PR. No upstream push is part of this report.

## Son Durum

#22 is complete locally.

Next likely plan item should be handled only after reading the project board
line exactly. GPU integration should not be started until the plan line calls
for it or the architecture item for external GPU PC is reached.
