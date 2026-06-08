# diarization-service

Faz 24 Meeting Intelligence — speaker diarization microservice (skeleton, #48).

FastAPI service that turns audio into **anonymous speaker turns** (who spoke
when). CPU-first PoC; the GPU decision follows the M5 performance work, mirroring
the live-stt discipline.

> **No voiceprint / biometric identity.** Speaker labels (`SPEAKER_00`, ...) are
> anonymous and per-request; speaker *identification* is a separate, consented,
> later phase.

## Backends

| `DIA_BACKEND` | Behaviour |
|---|---|
| `mock` (default) | Deterministic speaker turns — no model/token, runnable + unit-tested |
| `pyannote` | Real `pyannote.audio` pipeline — **stub** (returns 501); wiring needs torch + a Hugging Face token (follow-up) |

## API

- `POST /diarize` — multipart `audio` → `DiarizeResponse` (speaker segments)
- `GET /health` — liveness/readiness
- `GET /metrics` — Prometheus (`dia_*`, `kvkk_*`)

## Run (skeleton)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --port 8300
```

```bash
curl -X POST -F "audio=@sample.wav;type=audio/wav" http://localhost:8300/diarize
```

## Config (env, prefix `DIA_`)

`DIA_MODEL_NAME`, `DIA_DEVICE`, `DIA_BACKEND`, `DIA_MAX_AUDIO_MB`,
`DIA_MAX_SPEAKERS`, `DIA_REQUEST_TIMEOUT`, `DIA_MOCK_NUM_SPEAKERS`,
`DIA_MOCK_TURN_SEC`, `DIA_MOCK_DEFAULT_DURATION_SEC`. See `app/core/config.py`.

## Follow-ups (out of #48 skeleton scope)

- Wire the real `pyannote.audio` pipeline behind `DIA_BACKEND=pyannote`.
- GPU Dockerfile variant after the M5 hardware decision.
- Integration with `live-stt`/`final-stt` transcript alignment.
