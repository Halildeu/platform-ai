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

## Faz 24 #161 decision gate

`scripts/diar_decision_gate.py` is the source-side guard for selecting a
diarization backend. It consumes metadata-only candidate rows from
`diar_matrix.py`-style measurements, but synthetic/lab evidence cannot select a
backend. A pass requires approved pilot evidence, explicit DER/RTF/latency/VRAM
thresholds, approved license/deployment metadata, a `sha256:<64 hex>` evidence
hash, and explicit non-biometric posture:

- `voiceprint_enabled=false`
- `biometric_processing=false`
- `speaker_identity_mapping=false`

The current repo snapshot at `docs/evidence/diar-results-2026-06-17.jsonl` is
expected to remain `blocked` because it is synthetic-smoke evidence. This gate
does not process real audio, enable voiceprint, mutate runtime, prove
direct-STT/app-mTLS, or make a production readiness claim.
