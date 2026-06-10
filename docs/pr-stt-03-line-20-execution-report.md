# PR-stt-03 Line 20 Execution Report

Date: 2026-06-04

Repo: `C:\Users\zeynep.akkilic\Desktop\platform-ai-work`

Branch: `feature/pr-stt-03-subprocess-worker`

GitHub Project item: `#20 [PR-stt-03] Subprocess worker (multiprocessing.Process)`

## AG-019 Status

```text
AG-019 staging resource gate pending; implementation validated locally only.
```

#19 is still pending because this PC cannot resolve `staging-sw` and does not
have a `k3d-test` kubectl context. This #20 implementation was validated
locally and in Docker only. It must not be treated as staging-approved until
AG-019 is completed with real staging resource data.

## Kisa Karar

#20 implementation is complete locally.

The old in-process Whisper model + threading lock path was replaced with a
supervised worker pool facade. Production default now uses
`multiprocessing.Process` workers. Unit tests use an explicit inline backend so
the existing fake faster-whisper module remains in-process and no model is
downloaded during the default test suite.

## Plan #20 Ne Istiyordu?

GitHub Project #20 body:

```text
live-stt-service `TranscribeService`'i refactor — Whisper inference
`multiprocessing.Process` worker'inda calisir. Threading lock yerine
subprocess isolation:
- Worker pool config: max_workers env var
- Single-flight inference per worker
- Worker crash -> supervisor re-spawn
- model load once per worker (model cache reuse)

Codex iter-2 critical note: `asyncio.wait_for` worker thread leak cozer.
```

## Yapilan Is

### 1. Worker pool module eklendi

Yeni dosya:

```text
services/live-stt-service/app/services/worker.py
```

Icerik:

- `ProcessWorkerPool`
- `InlineWorkerPool`
- `WorkerPool` protocol
- `WorkerCrashedError`
- `_WorkerSlot`
- `_worker_main`
- `build_worker_pool`

### 2. TranscribeService facade haline getirildi

Degisen dosya:

```text
services/live-stt-service/app/services/transcribe.py
```

Eski davranis:

- Parent process icinde `WhisperModel` lazy-load ediliyordu.
- `threading.Lock` ile tek inference korunuyordu.
- Inference blocking sekilde ayni process icindeydi.

Yeni davranis:

- `TranscribeService` artik worker pool'a delegate ediyor.
- Production default: `ProcessWorkerPool`.
- Test backend: `InlineWorkerPool`.
- Public API degismedi: `transcribe(audio) -> TranscribeResponse`.

### 3. Worker config eklendi

Degisen dosya:

```text
services/live-stt-service/app/core/config.py
```

Yeni env/config:

```text
STT_WORKER_MAX_WORKERS
STT_WORKER_BACKEND
```

Default:

```text
STT_WORKER_MAX_WORKERS=1
STT_WORKER_BACKEND=process
```

Validation:

```text
worker_max_workers: 1..8
worker_backend: process | inline
```

### 4. Worker crash API mapping eklendi

Degisen dosya:

```text
services/live-stt-service/app/api/transcribe.py
```

Worker process response donmeden olurse parent `WorkerCrashedError` uretir.
API bunu client bad-audio gibi `400` yapmak yerine service-side failure olarak
`503` doner:

```text
HTTP 503
detail="STT worker crashed"
```

### 5. Test ortaminda inline backend kullanildi

Degisen dosya:

```text
services/live-stt-service/tests/conftest.py
```

Unit testlerde:

```text
STT_WORKER_BACKEND=inline
```

Gerekce:

Windows spawn multiprocessing child process, pytest'in `sys.modules`
monkeypatch'i ile eklenen fake `faster_whisper` module'unu otomatik tasimaz.
Bu nedenle default unit test suite'in model indirmesini onlemek icin explicit
inline backend kullanildi. Production default hala `process`.

## Plan Uyumu

| Plan maddesi | Durum | Not |
| --- | --- | --- |
| Whisper inference `multiprocessing.Process` worker'inda calisir | DONE | `ProcessWorkerPool` production default |
| Threading lock yerine subprocess isolation | DONE | Parent service artik model/lock tutmuyor |
| Worker pool config: max_workers env var | DONE | `STT_WORKER_MAX_WORKERS` |
| Single-flight inference per worker | DONE | Her `_WorkerSlot` queue `maxsize=1`, parent busy set/semaphore kullanir |
| Worker crash -> supervisor re-spawn | DONE | Dead slot detect edilir, `restart()` cagrilir |
| Model load once per worker | DONE | `_worker_main` icinde model `None` ise load edilir, sonra reuse |

## Kontrollu Tasarim Kararlari

### Inline backend neden var?

Bu production icin degil, test/dev guvenligi icin var.

Production default:

```text
STT_WORKER_BACKEND=process
```

Test default:

```text
STT_WORKER_BACKEND=inline
```

Bu sayede:

- Unit testler model indirmez.
- Existing fake faster-whisper mock korunur.
- Docker/integration tarafinda process backend gercekten calisir.

### #21 hard-kill neden yapilmadi?

#20 sadece subprocess worker refactor maddesidir.

#21 ayri plan maddesi:

```text
[PR-stt-03] Hard timeout kill semantic + worker re-start
```

Bu nedenle #20'de timeout olunca `worker.terminate()` / `worker.kill()` semantic
tam uygulanmadi. Worker process isolation temeli atildi; hard kill davranisi
#21'de eklenmelidir.

## Plandan Sapma Var Mi?

Kritik sapma yok.

Planin ana istegi `TranscribeService` icindeki Whisper inference'in parent
process/thread icinden cikarilip `multiprocessing.Process` worker'a alinmasiydi.
Bu yapildi.

Kontrollu tasarim kararlarini sapma olarak degerlendirmek gerekirse:

| Konu | Plan metni | Yapilan | Sapma etkisi |
| --- | --- | --- | --- |
| Worker backend | Process worker | Production default `process` | Uyumlu |
| Unit test backend | Plan belirtmiyor | `inline` test backend eklendi | Kontrollu test kolaylastirma; production davranisini degistirmez |
| Hard timeout kill | #21 konusu | #20'de yapilmadi | Sapma degil; plan sirasi korundu |
| Metrics | #20 body'de zorunlu degil | Yeni metric eklenmedi | Sapma degil; #21/#29 tarafinda genisletilebilir |
| Full ruff/mypy | Plan belirtmiyor | Targeted ruff PASS, mypy PASS | Full ruff eski repo borclarina takiliyor |

En onemli karar:

`STT_WORKER_BACKEND=inline` sadece test/dev icin var. Production default
`process` olarak kaldi. Bu nedenle asil plan hedefi bozulmadi.

## Dosya Dosya Ne Degisti?

### `app/services/worker.py`

Yeni eklendi.

Amaci:

- Worker process havuzunu yonetmek.
- Her worker icin single-flight inference saglamak.
- Worker process olurse yeniden baslatmak.
- Child process icinde Whisper modelini bir kere lazy-load etmek.
- Parent process'e sadece primitive dict/response tasimak.

Onemli siniflar/fonksiyonlar:

```text
ProcessWorkerPool
InlineWorkerPool
WorkerCrashedError
_WorkerSlot
_worker_main
build_worker_pool
```

### `app/services/transcribe.py`

Buyuk refactor yapildi.

Eski hali:

```text
TranscribeService
  -> parent process icinde WhisperModel yukler
  -> threading.Lock kullanir
  -> model.transcribe parent process icinde calisir
```

Yeni hali:

```text
TranscribeService
  -> worker pool facade
  -> build_worker_pool(settings)
  -> worker_pool.transcribe(audio)
```

Yani public API sade kaldi, agir is worker pool'a tasindi.

### `app/core/config.py`

Yeni config alanlari:

```text
STT_WORKER_MAX_WORKERS
STT_WORKER_BACKEND
```

Defaultlar:

```text
STT_WORKER_MAX_WORKERS=1
STT_WORKER_BACKEND=process
```

Neden:

- Plan `max_workers env var` istiyordu.
- Testlerde process yerine inline kullanmak icin backend secimi gerekti.

### `app/api/transcribe.py`

Yeni hata mapping:

```text
WorkerCrashedError -> HTTP 503
```

Neden:

Worker crash client'in kotu audio gondermesi degil, service-side failure'dir.
Bu nedenle `400` degil `503` donmelidir.

### `tests/conftest.py`

Unit test ortaminda:

```text
STT_WORKER_BACKEND=inline
```

Neden:

Pytest fake `faster_whisper` module'u parent process icinde monkeypatch ediyor.
Windows spawn child process bu monkeypatch'i otomatik tasimaz. Unit testlerde
gercek model indirmemek icin inline backend kullanildi.

### `tests/unit/test_worker_pool.py`

Yeni eklendi.

Test ettikleri:

- Default backend process mi?
- Inline backend secilebiliyor mu?
- `TranscribeService` worker pool'a delegate ediyor mu?
- `WorkerCrashedError` API mapping icin kullanilabilir mi?

### `tests/unit/test_config.py`

Yeni config alanlari test edildi:

```text
worker_backend default process
worker_max_workers default 1
env override
invalid backend reject
worker count bounds
```

### `tests/unit/test_api.py`

Worker crash mapping testi eklendi:

```text
WorkerCrashedError -> HTTP 503
```

### `tests/unit/test_transcribe_service.py`

Test settings icine:

```text
worker_backend="inline"
```

eklendi.

## Riskler

| Risk | Etki | Mitigasyon |
| --- | --- | --- |
| Multiprocessing Windows/Linux davranis farki | Child process spawn davranisi farkli olabilir | Windows unit/integration + Docker smoke calisti |
| Worker process model load cold start maliyeti | Ilk request yine yavas olabilir | #17 baseline zaten cold start'i dokumante etti |
| Worker crash respawn var ama hard timeout kill yok | Timeout durumunda tam kill semantic henuz eksik | #21 explicitly bu is icin ayrildi |
| Worker count fazla verilirse RAM baskisi | Her worker model yukleyecegi icin memory artar | `STT_WORKER_MAX_WORKERS` 1..8 bound; AG-019 pending notu |
| Inline backend yanlislikla production'da kullanilirsa isolation kaybolur | Production subprocess isolation devre disi kalir | Default `process`; README/raporda production default yazildi |
| AG-019 tamamlanmadan staging kapasitesi bilinmiyor | Worker ayarlari staging'e agir gelebilir | Her rapora AG-019 pending notu eklendi; upstream/deploy oncesi #19 kapanmali |

## Geri Donus / Rollback Plani

Eger #20 degisikligi beklenmeyen sorun cikarirsa geri donus basit:

1. `TranscribeService` eski parent-process lazy-load + lock yapisina geri
   alinabilir.
2. `app/services/worker.py` devre disi birakilir.
3. `STT_WORKER_BACKEND` / `STT_WORKER_MAX_WORKERS` config alanlari kaldirilir
   veya `inline` backend gecici default yapilir.
4. API mapping icindeki `WorkerCrashedError` path'i kaldirilir.

Ancak mevcut dogrulama sonucunda rollback gerektiren bir bulgu yoktur:

```text
unit tests PASS
integration tests PASS
Docker smoke PASS
```

## Ne Yapilmadi?

Bu adimda bilincli olarak sunlar yapilmadi:

- Hard timeout kill semantic yapilmadi; #21'in konusu.
- `stt_worker_killed_total` metric eklenmedi; #21 ile daha dogru.
- Redis queue consumer'a dokunulmadi; PR-queue maddesi.
- GPU worker parallelism eklenmedi; PR-gpu maddesi.
- Approved GPU live PoC repo icine entegre edilmedi; bu baska plan maddesi.

## Degisen Dosyalar

Kod:

```text
services/live-stt-service/app/services/worker.py
services/live-stt-service/app/services/transcribe.py
services/live-stt-service/app/core/config.py
services/live-stt-service/app/api/transcribe.py
```

Test:

```text
services/live-stt-service/tests/conftest.py
services/live-stt-service/tests/unit/test_api.py
services/live-stt-service/tests/unit/test_config.py
services/live-stt-service/tests/unit/test_transcribe_service.py
services/live-stt-service/tests/unit/test_worker_pool.py
```

Rapor:

```text
docs/pr-stt-03-line-20-execution-report.md
```

## Test ve Dogrulama

### Syntax

Komut:

```bash
python -m py_compile app/core/config.py app/services/worker.py app/services/transcribe.py app/api/transcribe.py
```

Sonuc:

```text
PASS
```

### Unit tests

Komut:

```bash
python -m pytest -m "not integration" -q
```

Sonuc:

```text
45 passed, 3 deselected, 4 xfailed, 7 xpassed, 1 warning
```

Not:

`xfailed/xpassed` PII redaction testleri bu PR kapsamindan once var olan
Issue #97 notlu test durumudur.

### Integration tests

Komut:

```bash
python -m pytest -m integration -q
```

Sonuc:

```text
3 passed, 56 deselected
```

Bu test production default process backend ile gercek audio path'i dogruladi.

### Docker build

Komut:

```bash
docker build -t live-stt-service:dev .
```

Sonuc:

```text
PASS
```

### Docker smoke

Komut:

```bash
bash scripts/docker-smoke.sh --skip-build
```

Sonuc:

```text
Docker smoke PASS
language: tr
duration: 5.52s
segments: 1
wall-clock: 30s
stt_transcribe_total: 1.0
```

### Ruff

Targeted command for #20-touched new worker/test files:

```bash
python -m ruff check app/services/worker.py tests/unit/test_worker_pool.py tests/conftest.py
```

Sonuc:

```text
All checks passed.
```

Full repo command:

```bash
python -m ruff check app tests
```

Sonuc:

```text
FAILED
```

Sebep:

Full ruff mevcut repo borclarina takiliyor:

- pre-existing Turkish ambiguous-character RUF001/RUF002 warnings
- pre-existing long lines in `app/api/transcribe.py`
- pre-existing import/unused warnings in older test code

#20 kapsamindaki yeni worker dosyasi ve yeni worker test dosyasi targeted ruff
kontrolunden gecti.

### Mypy

Komut:

```bash
python -m mypy app
```

Sonuc:

```text
Success: no issues found in 13 source files
```

Ilk calistirmada `mypy` kurulu degildi. Kurulduktan sonra `worker.py`,
`api/transcribe.py` ve `main.py` icindeki tip annotation sorunlari giderildi.
Son durumda app kaynak kodu mypy'dan temiz geciyor.

## Bilinen Limitler

- #20 process isolation temelini ekler.
- #21 hard timeout kill semantics hala yapilmadi.
- Parent process, worker timeout olursa child'i kill etme davranisini #21'de
  tamamlamali.
- AG-019 staging resource gate pending oldugu icin bu sonuc staging-approved
  degildir.

## Halil Bey Reposuna Etki

Yok.

Bu calismada:

- `upstream/Halildeu/platform-ai` icin push yapilmadi.
- PR acilmadi.
- Halil Bey'in GitHub reposuna dokunulmadi.

## Son Durum

#20 local/fork seviyesinde tamamlandi.

Sıradaki plan maddesi #21'dir:

```text
[PR-stt-03] Hard timeout kill semantic + worker re-start
```

Ancak #21'e gecmeden once bu #20 branch'i commitlenip sadece fork'a
pushlanabilir. Upstream PR/merge/deploy icin AG-019 (#19) staging resource gate
kapanmadan ilerlenmemelidir.
