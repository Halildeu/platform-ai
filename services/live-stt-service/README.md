# live-stt-service

Faz 24 Meeting Intelligence — **canlı (live) Whisper STT** mikroservisi (PoC iskelet).

## Sorumluluk

Kısa ses chunk'larını (2-10 sn) hızlı geçici (draft) transcript'e çevirir.

PoC scope:
- Senkron HTTP `POST /transcribe` (multipart audio file)
- faster-whisper `medium int8` CPU
- Türkçe (`STT_LANGUAGE=tr` default)

Sonraki sliceler:
- WebSocket streaming (chunk-by-chunk, draft → final state machine)
- GPU variant (`large-v3-turbo` int8/float16)
- Live segment merge + revize
- Redis queue tarafına push (audio-gateway-service ile entegre)

## Yapı

```
app/
├── core/config.py            # Pydantic Settings (env STT_*)
├── models/schemas.py         # request/response Pydantic
├── services/transcribe.py    # Whisper wrapper (lazy-load + lock)
└── api/
    ├── health.py             # GET /health
    └── transcribe.py         # POST /transcribe
tests/
├── conftest.py               # mock faster_whisper (no model download in CI)
└── unit/
    ├── test_config.py
    ├── test_transcribe_service.py
    └── test_api.py
Dockerfile                    # CPU multi-stage (ffmpeg + python:3.11-slim)
pyproject.toml                # ruff / mypy / black / pytest config
requirements.txt              # runtime pin
requirements-dev.txt          # + pytest/mypy/ruff/black/bandit
```

## Local dev

```bash
# Python 3.11+ venv
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Test (mock Whisper — no model download)
pytest -v

# Type + lint
ruff check app/ tests/
mypy app/
black --check app/ tests/

# Local server (ilk request medium modeli indirir ~1.5 GB, dakikalar)
export STT_MODEL_NAME=medium STT_LANGUAGE=tr STT_DEVICE=cpu STT_COMPUTE_TYPE=int8
uvicorn app.main:app --host 0.0.0.0 --port 8200 --reload

# Smoke
curl -F "audio=@sample-tr.wav" http://localhost:8200/transcribe | jq
curl http://localhost:8200/health | jq
```

## Docker

```bash
docker build -t live-stt-service:dev services/live-stt-service
docker run --rm -p 8200:8200 \
  -v $HOME/.cache/huggingface:/home/stt/.cache/huggingface \
  -e STT_MODEL_NAME=medium -e STT_LANGUAGE=tr \
  live-stt-service:dev
```

### NVIDIA GPU image (#41)

GPU image, CPU image'dan ayrı tutulur:

```powershell
cd services/live-stt-service
docker build -f Dockerfile.gpu -t live-stt-service:gpu-issue-41 .

docker run --rm --gpus all -p 8200:8200 `
  -e STT_MODEL_NAME=medium `
  -e STT_LANGUAGE=tr `
  -e STT_DEVICE=cuda `
  -e STT_COMPUTE_TYPE=float16 `
  live-stt-service:gpu-issue-41
```

Image tabanı:

```text
nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04
CUDA: 12.2.2
cuDNN: 8
CTranslate2: 4.8.0
```

GPU host'ta NVIDIA Container Toolkit / Docker `--gpus all` desteği
zorunludur. Fail-fast gerçek GPU smoke:

```powershell
.\scripts\gpu-smoke.ps1 `
  -Image live-stt-service:gpu-issue-41 `
  -Model medium `
  -ComputeType float16
```

Script şunları doğrular:

- `nvidia-smi` container içinden GPU'yu görür;
- CTranslate2 en az bir CUDA device ve desteklenen compute type'ları raporlar;
- `libcublas.so.12` ve `libcudnn.so.8` runtime'da bulunur;
- FFmpeg CUDA hwaccel, NVDEC/CUVID ve NVENC varlığı opsiyonel olarak raporlanır;
- gerçek Türkçe WAV, servis `/transcribe` yolundan işlenir;
- response `device=cuda` ve seçilen compute type bilgisini taşır;
- image boyutu, inference süresi ve non-root UID/GID raporlanır.

`float16` varsayılandır. `int8_float16`, aynı scriptte `-ComputeType
int8_float16` ile ayrıca ölçülebilir. Bu iki modun kalite/latency/VRAM
karşılaştırması #43 performans matrisi kapsamındadır.

NVDEC/NVENC zorunlu STT bağımlılığı değildir; ses decode CPU ffmpeg yoluyla
yapılır. GPU video decode/encode yalnızca ileride video kaynakları eklendiğinde
opsiyonel hızlandırmadır.

## PR-stt-02 baseline

Project item #17 measured the current PoC with a real Turkish Common Voice
fixture in a CPU Docker container.

Baseline summary:

| Metric | Value |
|---|---:|
| Model | `medium` |
| Device | `cpu` |
| Compute type | `int8` |
| Cold start total | `40.097687s` |
| Cold API `elapsed_ms` | `10872ms` |
| Approx. model load + overhead | `29.226s` |
| Warm transcribe wall-clock | `7.718536s` |
| Warm API `elapsed_ms` | `7701ms` |
| Peak observed container memory | `1.503GiB` |
| Docker smoke | PASS |
| Integration test | `3 passed, 50 deselected` |

Full report:

- `docs/poc-stt-baseline.md`
- `docs/pr-stt-02-line-17-execution-report.md`

## Faz 24 T-B G-WER quality gate

`scripts/wer_matrix.py` and `services/diarization-service/scripts/diar_matrix.py`
produce model/diarization measurement rows. Those rows are evidence inputs, but
Common Voice and synthetic-smoke rows do not satisfy the product-quality gate by
themselves. The gate verifier requires explicit pilot-meeting WER + DER rows and
operator-provided thresholds:

```bash
cd services/live-stt-service
python scripts/gwer_gate.py \
  --wer-evidence ../../docs/evidence/wer-results-2026-06-10.jsonl \
  --der-evidence ../../docs/evidence/diar-results-2026-06-17.jsonl \
  --max-wer 0.25 \
  --max-der 0.30
```

Current repository evidence is expected to return `blocked` because it contains
Common Voice and synthetic-smoke measurements, not approved pilot-meeting WER/DER
evidence. `pass` is reserved for metadata-only pilot evidence under explicit
thresholds. The verifier rejects raw audio paths, transcript/reference text, and
hypothesis text in evidence rows.

## Faz 24 G-LAT/COST quality gate

`scripts/perf_client.py`, `scripts/saturation_stats.py`, and `scripts/cost.py`
produce the raw measurement inputs for latency, queue lag, throughput,
utilization, and per-audio-minute cost. The acceptance gate is deliberately
separate from those helpers: lab or synthetic performance rows can support
capacity planning, but they cannot prove the product gate without approved pilot
evidence and explicit thresholds.

```bash
cd services/live-stt-service
python scripts/glat_cost_gate.py \
  --evidence ../../docs/evidence/latcost-results-2026-06-25.jsonl \
  --max-latency-p95-ms 2500 \
  --max-queue-lag-p95-ms 500 \
  --max-cost-per-audio-minute 0.01 \
  --max-realtime-factor 0.35 \
  --max-error-rate 0.01 \
  --min-audio-minutes 30 \
  --min-audio-minutes-per-wall-hour 600
```

Current repository evidence is expected to return `blocked` because it is a
legacy performance-lab row, not approved pilot-meeting evidence. `pass` is
reserved for metadata-only pilot evidence that includes latency p50/p95, queue
lag p95, real-time factor, throughput, utilization, cost per audio minute, error
rate, and a redacted evidence hash under explicit thresholds. The verifier
rejects raw audio paths, transcript text, fixture paths, prompts/responses, and
PII-shaped strings in evidence rows.

## Known limits and open blockers

This section documents Project item #18:
`[PR-stt-02] README known-limits + open blocker notu`.

Known limits:

- CPU-only PoC: current Docker baseline is `medium/int8/cpu`.
- No GPU in this PR line: GPU variants such as `large-v3`, `large-v3-turbo`,
  CUDA, and float16 are not part of this baseline.
- No client WebSocket: current official service surface is synchronous
  `POST /transcribe`, not browser/mobile live streaming.
- No production exposure: do not expose this service directly to users or the
  internet; Gateway, auth, rate limits, queueing, and deployment hardening are
  separate plan items.
- No hard kill isolation yet: inference currently runs behind an async timeout
  and threadpool path, but the worker is not isolated in a killable subprocess.
- Timeout worker leak remains open: if a Whisper inference exceeds
  `STT_REQUEST_TIMEOUT`, the API returns 504, but the underlying worker thread
  can continue until the blocking inference exits.
- Redis Streams consumer exists only as an opt-in **control-plane** reader:
  it validates hash+metadata envelopes on `audio:chunks:p00..p31`, dedups by
  `messageId`, XACKs poison/duplicate/success messages, trims bounded streams,
  and recovers stale pending entries with XAUTOCLAIM. It does **not** fetch
  audio bytes or transcribe from Redis; the hash-to-audio fetch + transcription
  handler is a later #182/#188 runtime slice.
- No production WER claim: Common Voice fixtures are smoke/baseline inputs, not
  an accuracy benchmark for Turkish enterprise meetings.
- No iPhone-like live dictation claim: this CPU sync endpoint is for baseline
  measurement. Real live UX requires the approved GPU streaming architecture or
  a later optimized streaming ASR worker.

Open blocker:

- `timeout worker leak` is intentionally left for PR-stt-03, where
  `TranscribeService` should move Whisper inference into a supervised
  `multiprocessing.Process` worker. That worker can be killed and respawned
  deterministically after timeout or crash.

3-AI consensus reference:

- Codex `019e879c` + Mavis `78 AGREE`.

Approved GPU live PoC note:

- The separately approved GPU live STT PoC is not represented by the CPU
  baseline numbers above. That PoC uses WebSocket streaming, a fast draft model,
  and a larger final model on the GPU server. Integration of that path into the
  official repository should be handled by a later plan item, not by PR-stt-02
  README documentation.

## Konfigürasyon (env)

| Variable | Default | Anlam |
|---|---|---|
| `STT_MODEL_NAME` | `medium` | Whisper model — `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` |
| `STT_COMPUTE_TYPE` | `int8` | Quantization — `int8`/`int8_float16`/`float16`/`float32` |
| `STT_DEVICE` | `cpu` | `cpu`/`cuda`/`auto` |
| `STT_LANGUAGE` | `tr` | ISO 639-1 veya `auto` |
| `STT_BEAM_SIZE` | `5` | Whisper beam (1-10) |
| `STT_VAD_FILTER` | `true` | Whisper built-in VAD |
| `STT_MAX_AUDIO_MB` | `50` | DoS guard (1-500) |
| `STT_LOG_LEVEL` | `INFO` | logging level |
| `STT_REQUEST_TIMEOUT` | `60` | hard cap sec |

## D-disiplin

- Model değişimi (ör. medium → large) ayrı ADR + PoC ölçüm + Codex consensus gerektirir
- Whisper version pin'i `requirements.txt`'te; major bump için Codex review
- Ses dosyası logging YASAK — `extra` field'a sadece meta (duration, elapsed, segment count, language)
- KVKK: ses içeriği Redis/PG'ye yazılmaz; sadece transcript text + meta

## Sıradaki

1. PoC integration test (gerçek Türkçe wav fixture + medium model)
2. Local docker compose ile e2e smoke (audio file → curl → transcript)
3. WebSocket streaming slice (`/ws/stream` — chunk-by-chunk + state machine)
4. Hash-only Redis control-plane runtime activation after #188/#182 gates
5. Prometheus `/metrics` endpoint + Grafana dashboard
6. GitOps deploy (platform-k8s-gitops kustomize base + overlay)
