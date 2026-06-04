# PR-stt-03 Line 21 Execution Report

Date: 2026-06-04

Repo: `C:\Users\zeynep.akkilic\Desktop\platform-ai-work`

Branch: `feature/pr-stt-03-hard-timeout-kill`

GitHub Project item: `#21 [PR-stt-03] Hard timeout kill semantic + worker re-start`

## AG-019 Status

```text
AG-019 staging resource gate pending; implementation validated locally only.
```

#19 is still pending because this PC cannot resolve `staging-sw` and does not
have a `k3d-test` kubectl context. This #21 implementation was validated
locally and in Docker only. It must not be treated as staging-approved until
AG-019 is completed with real staging resource data.

## Kisa Karar

#21 implementation is complete locally.

Timeout is now handled at the process-worker layer for the production
`process` backend. If a worker exceeds the configured request timeout, the
parent process terminates it, waits the configured grace period, kills it if it
is still alive, respawns a fresh worker, records a timeout-kill metric, and the
API returns controlled HTTP 504.

## Plan #21 Ne Istiyordu?

GitHub Project #21 body:

```text
Timeout asildiginda:
1. worker.terminate() (SIGTERM)
2. Grace period 2 sn
3. worker.kill() (SIGKILL)
4. Pool re-spawn yeni worker
5. Metric stt_worker_killed_total{reason=timeout} increment
6. Controlled failure response (504)

ProcessPoolExecutor cancel() yetmez — Codex iter-1 not.
```

## Yapilan Is

### 1. Worker timeout exception eklendi

Degisen dosya:

```text
services/live-stt-service/app/services/worker.py
```

Yeni exception:

```text
WorkerTimeoutError
```

Bu exception sadece timeout kill/respawn yolu tamamlandiktan sonra raise edilir.

### 2. Worker-level timeout eklendi

`ProcessWorkerPool.transcribe(...)` artik opsiyonel timeout aliyor:

```text
timeout_sec: float | None
```

Process backend'de API bu alana `STT_REQUEST_TIMEOUT` degerini gonderir.

Timeout akisi:

```text
deadline = now + timeout_sec
while waiting for result:
  if worker died -> crash path
  if deadline exceeded:
    slot.kill_for_timeout(grace)
    raise WorkerTimeoutError
```

### 3. terminate -> grace -> kill -> respawn eklendi

`_WorkerSlot.kill_for_timeout(...)` eklendi.

Davranis:

```text
if process alive:
  process.terminate()
  process.join(grace_sec)
  if still alive:
    process.kill()
    process.join(grace_sec)

process = None
task_queue = new Queue(maxsize=1)
result_queue = new Queue(maxsize=1)
model_loaded = False
start new worker process
```

Bu planin ilk dort maddesini karsilar:

```text
worker.terminate()
Grace period
worker.kill()
Pool re-spawn yeni worker
```

### 4. Kill grace config eklendi

Degisen dosya:

```text
services/live-stt-service/app/core/config.py
```

Yeni config:

```text
STT_WORKER_KILL_GRACE_SEC
```

Default:

```text
2.0
```

Validation:

```text
0.0 <= worker_kill_grace_sec <= 30.0
```

Plan 2 saniye grace istedigi icin default 2.0 yapildi. Env ile degisebilir
yapmak kontrollu bir genisletmedir; plan davranisini bozmaz.

### 5. Metric eklendi

Degisen dosya:

```text
services/live-stt-service/app/api/metrics.py
```

Yeni metric:

```text
stt_worker_killed_total{reason="timeout"}
```

Bu metric `WorkerTimeoutError` yakalandiginda increment edilir.

### 6. API timeout path duzeltildi

Degisen dosya:

```text
services/live-stt-service/app/api/transcribe.py
```

Eski process path riski:

```text
asyncio.wait_for(run_in_threadpool(service.transcribe), timeout=...)
```

Bu API 504 donse bile threadpool icindeki blocking call arkada beklemeye devam
edebilirdi. #21'in cozmek istedigi ana risk buydu.

Yeni process backend path:

```text
result = await run_in_threadpool(
    service.transcribe,
    BytesIO(raw),
    settings.request_timeout,
)
```

Burada timeout worker pool icinde uygulanir. Worker kill/respawn tamamlanmadan
`WorkerTimeoutError` raise edilmez.

Inline backend path:

```text
asyncio.wait_for(...)
```

Inline backend test/dev icindir. Patch edilen sleep testlerini desteklemek icin
outer wait_for korunur. Production default `process` oldugu icin planin worker
kill hedefi production path'te uygulanir.

### 7. Controlled 504 response eklendi

API catch path:

```text
except (asyncio.TimeoutError, TimeoutError, WorkerTimeoutError)
```

Response:

```text
HTTP 504
detail = "Transcribe exceeded <timeout>s timeout (...)"
```

Worker timeout ise metric de artar:

```text
stt_worker_killed_total.labels(reason="timeout").inc()
```

## Plan Uyumu

| Plan maddesi | Durum | Kanit |
| --- | --- | --- |
| `worker.terminate()` | DONE | `_WorkerSlot.kill_for_timeout` |
| Grace period 2 sn | DONE | `STT_WORKER_KILL_GRACE_SEC=2.0` default |
| `worker.kill()` | DONE | terminate sonrasi alive ise `process.kill()` |
| Pool re-spawn yeni worker | DONE | queue reset + `start()` |
| `stt_worker_killed_total{reason=timeout}` | DONE | metrics + API increment |
| Controlled failure response 504 | DONE | API timeout catch |

## Plandan Sapma Var Mi?

Kritik sapma yok.

Kontrollu tasarim kararlari:

| Konu | Plan metni | Yapilan | Etki |
| --- | --- | --- | --- |
| Grace period | 2 sn | Default 2.0 sec + env override | Uyumlu, daha operasyonel |
| Timeout yeri | Worker kill semantic | Process backend timeout worker pool icinde | Uyumlu |
| Inline backend | Plan belirtmiyor | Test/dev icin outer wait_for korunur | Production davranisini degistirmez |
| Metric increment yeri | Worker killed metric | API catch path increment eder | WorkerTimeoutError sadece kill/respawn sonrasi geldigi icin uyumlu |

GPU veya approved live STT entegrasyonu yapilmadi. Bu #21'in kapsami degildir.

## Yasanan Sorun ve Cozum

Targeted unit test sirasinda Python 3.10 uyumluluk farki yakalandi.

Bulgu:

```text
asyncio.TimeoutError is TimeoutError -> False
```

Bu PC'deki Python 3.10 ortaminda `asyncio.wait_for(...)` tarafindan firlatilan
`asyncio.TimeoutError`, builtin `TimeoutError` ile ayni sinif degildir. Bu
yuzden inline test/dev backend path'inde timeout ilk denemede 504'e map
edilmeden testten disari cikti.

Cozum:

```text
except WorkerTimeoutError as exc:
  # worker kill + metric path
except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
  # normal controlled timeout path
```

Bu bir plan sapmasi degildir. Production `process` backend icin #21'in ana
hedefi olan worker terminate -> grace -> kill -> respawn akisi korunur. Eklenen
`asyncio.TimeoutError` yakalama sadece test/dev inline path'in Python 3.10'da da
kontrollu 504 donmesini saglar.

## Dosya Dosya Ne Degisti?

### `app/services/worker.py`

Eklenenler:

- `WorkerTimeoutError`
- `timeout_sec` parametresi
- deadline hesaplama
- `_WorkerSlot.kill_for_timeout`
- terminate -> grace -> kill -> respawn akisi

### `app/services/transcribe.py`

`TranscribeService.transcribe(...)` artik `timeout_sec` parametresini worker
pool'a aktarir.

### `app/api/transcribe.py`

Process backend icin worker-level timeout path eklendi.

Inline backend icin eski async timeout path korundu.

`WorkerTimeoutError` 504'e map edildi.

`stt_worker_killed_total{reason=timeout}` increment edildi.

### `app/api/metrics.py`

Yeni counter:

```text
stt_worker_killed_total
```

### `app/core/config.py`

Yeni env:

```text
STT_WORKER_KILL_GRACE_SEC
```

### `tests/unit/test_worker_pool.py`

Eklenen test:

```text
test_process_worker_timeout_kills_and_respawns_slot
```

Fake slot ile timeout yolunda `kill_for_timeout` cagrildigi ve task'in
transcribe olarak queue'ya yazildigi dogrulandi.

### `tests/unit/test_api.py`

Eklenen test:

```text
test_transcribe_worker_timeout_maps_to_504
```

### `tests/unit/test_config.py`

`worker_kill_grace_sec` default, env override ve bounds test edildi.

## Test ve Dogrulama

### Syntax

```bash
python -m py_compile app/core/config.py app/api/metrics.py app/services/worker.py app/services/transcribe.py app/api/transcribe.py
```

Sonuc:

```text
PASS
```

### Mypy

```bash
python -m mypy app
```

Sonuc:

```text
Success: no issues found in 13 source files
```

### Unit tests

```bash
python -m pytest -m "not integration" -q
```

Sonuc:

```text
47 passed, 3 deselected, 4 xfailed, 7 xpassed, 1 warning
```

`xfailed/xpassed` PII redaction testleri bu PR kapsamindan once var olan Issue
#97 notlu test durumudur.

### Integration tests

```bash
python -m pytest -m integration -q
```

Sonuc:

```text
3 passed, 58 deselected
```

### Targeted ruff

```bash
python -m ruff check app/services/worker.py tests/unit/test_worker_pool.py app/api/metrics.py app/api/transcribe.py
```

Sonuc:

```text
All checks passed.
```

### Docker build

```bash
docker build -t live-stt-service:dev .
```

Sonuc:

```text
PASS
```

### Docker smoke

```bash
bash scripts/docker-smoke.sh --skip-build
```

Sonuc:

```text
Docker smoke PASS
language: tr
duration: 5.52s
segments: 1
wall-clock: 50s
stt_transcribe_total: 1.0
```

Son tekrar dogrulamada Docker smoke yine PASS verdi ve wall-clock `23s`
olculdu. Onceki `50s` degeri cold-start/cache durumundan kaynakli daha yavas
bir kosuydu; iki sonuc da basarili smoke dogrulamasidir.

## Riskler

| Risk | Etki | Mitigasyon |
| --- | --- | --- |
| Windows/Linux process kill farklari | terminate/kill davranisi platforma gore degisebilir | Windows unit/integration + Docker Linux smoke calisti |
| Worker timeout gercek model load sirasinda tetiklenirse | Cold start worker kill yiyebilir | `STT_REQUEST_TIMEOUT` production'da yeterli yuksek tutulmali |
| Grace sec cok dusuk verilirse | Worker kill daha agresif olur | Default 2.0, env bounds 0..30 |
| Metric API catch path'te increment ediliyor | Worker metric yalniz WorkerTimeoutError gelirse artar | WorkerTimeoutError kill/respawn sonrasi raise ediliyor |
| AG-019 pending | Staging kaynak etkisi bilinmiyor | Upstream/deploy oncesi #19 kapanmali |

## Ne Yapilmadi?

Bu adimda bilincli olarak sunlar yapilmadi:

- GPU worker veya CUDA parallelism eklenmedi.
- Approved GPU live STT PoC repo icine entegre edilmedi.
- Redis queue consumer'a dokunulmadi.
- Full production SLO/alert rule eklenmedi; observability PR'larinda genisler.

## Geri Donus / Rollback Plani

Sorun cikarsa geri donus:

1. Process backend timeout path'ten `timeout_sec` parametresi kaldirilir.
2. `_WorkerSlot.kill_for_timeout` devre disi birakilir.
3. `stt_worker_killed_total` metric kullanimi kaldirilir.
4. API tekrar outer `asyncio.wait_for` path'ine alinabilir.

Ancak mevcut dogrulamalarda rollback gerektiren bulgu yoktur.

## Halil Bey Reposuna Etki

Yok.

Bu calismada:

- `upstream/Halildeu/platform-ai` icin push yapilmadi.
- PR acilmadi.
- Halil Bey'in GitHub reposuna dokunulmadi.

## Son Durum

#21 local/fork seviyesinde tamamlandi.

Siradaki plan maddesi #22'dir:

```text
[PR-stt-03] Test: timeout -> kill -> metric -> controlled failure
```

Not: #21 icinde temel timeout kill testleri eklendi. #22 muhtemelen bu davranisi
daha kontrat seviyesinde genisletip metric/controlled failure kanitini
sertlestirecek.
