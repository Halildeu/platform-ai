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
4. Redis queue producer (audio-gateway-service tarafından consume)
5. Prometheus `/metrics` endpoint + Grafana dashboard
6. GitOps deploy (platform-k8s-gitops kustomize base + overlay)
