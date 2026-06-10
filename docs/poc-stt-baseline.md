# PoC STT Baseline Metrics

Date: 2026-06-04

Scope: PR-stt-02 / Project item #17, "Memory peak + model load + transcribe latency baseline olcum".

## Environment

- Repo: `platform-ai`
- Service: `services/live-stt-service`
- Docker image: `live-stt-service:dev`
- Runtime: Docker Desktop, CPU container
- Model: `medium`
- Language: `tr`
- Compute type: `int8`
- Device: `cpu`
- Request timeout used by smoke/baseline: `STT_REQUEST_TIMEOUT=180`
- Fixture source: Common Voice 17 Turkish mirror fallback, `fsicoli/common_voice_17_0`

## Fixtures

The upstream `mozilla-foundation/common_voice_17_0` Hugging Face dataset listing did not expose audio files through `datasets` during this run. The downloader now tries that canonical dataset first and falls back to `fsicoli/common_voice_17_0`.

Generated fixtures:

| File | Expected text |
| --- | --- |
| `services/live-stt-service/tests/fixtures/sample-tr-cv17-001.wav` | `Gecis ulkelerinde yasananlar ise karisik.` |
| `services/live-stt-service/tests/fixtures/sample-tr-cv17-002.wav` | `Iki halterci Pekin'de de altin icin yarisacak.` |

The `.txt` sidecar files keep the original Turkish text in UTF-8.

## Baseline Results

### Docker Smoke

Command:

```bash
bash scripts/docker-smoke.sh --skip-build
```

Result:

- `/health`: reachable
- `/transcribe`: HTTP 200
- `/metrics`: HTTP 200
- `stt_transcribe_total`: `1.0`
- Smoke result: PASS
- Smoke wall-clock for fixture 1: `30s`
- Reported audio duration: `5.52s`
- Reported language: `tr`
- Reported segments: `1`
- Reported text: `Gecis ulkelerinde yasananlar ise karisik.`

### Cold Start

Cold start includes lazy Whisper model load plus the first transcription.

| Metric | Value |
| --- | ---: |
| Curl total time | `40.097687s` |
| API `elapsed_ms` | `10872ms` |
| Approx. model load + overhead | `29.226s` |
| HTTP status | `200` |
| Text | `Gecis ulkelerinde yasananlar ise karisik.` |

### Warm Transcribe

Warm transcribe uses the same container after the model is already loaded.

| Metric | Value |
| --- | ---: |
| Curl total time | `7.718536s` |
| API `elapsed_ms` | `7701ms` |
| HTTP status | `200` |
| Metrics counter after two requests | `stt_transcribe_total{language="tr",model="medium",result="success"} 2.0` |

### Memory

Peak memory observed during the first request sampling run:

| Metric | Value |
| --- | ---: |
| Peak observed container memory | `1.503GiB` |
| Container memory limit reported by Docker | `7.753GiB` |
| Warm post-request memory snapshot | `1.293GiB / 7.753GiB` |

Sampling command used Docker stats while the request was in flight:

```bash
docker stats --no-stream --format '{{.MemUsage}}' live-stt-baseline-17
```

## Verification

Commands run:

```bash
python -m pytest -m integration -q
docker build -t live-stt-service:dev .
bash scripts/docker-smoke.sh --skip-build
```

Results:

- Integration tests: `3 passed, 50 deselected`
- Docker build: PASS
- Docker smoke: PASS

## Notes

- The model is lazy-loaded on the first `/transcribe` request. Cold start is therefore much slower than warm requests.
- `docker-smoke.sh` mounts the host Hugging Face cache. On Windows/WSL it now detects the Windows user profile cache instead of defaulting to `/root/.cache/huggingface`.
- `requests` is included in runtime dependencies because the `/transcribe` path needs it through the packaged stack inside the Docker image.
- Logging now adds a default `correlation_id` only for records that do not have one. This prevents third-party logs from breaking the configured structured log format.

## Current Baseline Interpretation

This CPU Docker baseline is acceptable for PoC measurement and regression tracking, but it is not an iPhone-like live STT target. The measured warm latency is around `7.7s` for a `5.5s` sample with `medium/int8/cpu`. Real live UX still requires the approved GPU streaming path or a later optimized streaming ASR worker.
