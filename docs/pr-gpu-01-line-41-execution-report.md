# PR-gpu-01 Line #41 Execution Report

Issue: `#41 [PR-gpu-01] Dockerfile.gpu (NVIDIA CUDA + cuDNN + faster-whisper CUDA)`

Canonical source: live GitHub issue body read on 2026-06-08.

## Objective

Build and validate a dedicated NVIDIA GPU image for `live-stt-service` with:

- NVIDIA CUDA 12.2 runtime on Ubuntu 22.04;
- cuDNN;
- faster-whisper with `float16` and `int8_float16`;
- ffmpeg;
- optional GPU video capability detection;
- reproducible image size evidence.

The target development GPU is the existing RTX 4070 PC documented in issue
#40. This does not make that host the final production hardware.

## Requirement Mapping

| Live issue requirement | Implementation |
|---|---|
| `nvidia/cuda:12.2-runtime-ubuntu22.04` | Digest-pinned `12.2.2-cudnn8-runtime-ubuntu22.04` |
| cuDNN install | Official NVIDIA cuDNN 8 runtime image |
| faster-whisper float16 | Default `STT_COMPUTE_TYPE=float16` |
| faster-whisper int8_float16 | Smoke parameter supports `int8_float16` |
| ffmpeg | Installed from Ubuntu repository |
| optional hardware video decode | CUDA hwaccel and CUVID/NVDEC probe |
| optional NVENC | NVENC encoder probe |
| image size baseline | Smoke script records Docker image byte size |

## Image Design

File:

```text
services/live-stt-service/Dockerfile.gpu
```

Base:

```text
nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04
digest:
sha256:2d913b09e6be8387e1a10976933642c73c840c0b735f0bf3c28d97fc9bc422e0
```

Runtime defaults:

```text
STT_DEVICE=cuda
STT_COMPUTE_TYPE=float16
STT_WORKER_MAX_WORKERS=1
NVIDIA_VISIBLE_DEVICES=all
NVIDIA_DRIVER_CAPABILITIES=compute,utility,video
```

The image preserves the official service architecture:

- FastAPI HTTP service;
- supervised subprocess worker;
- lazy Whisper model loading;
- one Uvicorn worker;
- non-root UID/GID `10001:10001`;
- existing `/health`, `/transcribe` and `/metrics` contracts.

## Dependency Pinning

`requirements-gpu.txt` includes the normal runtime requirements and explicitly
pins:

```text
ctranslate2==4.8.0
```

This prevents a floating transitive CTranslate2 upgrade from silently changing
CUDA/cuDNN compatibility.

Build-time checks fail if:

- `ctranslate2`, `faster_whisper` or `requests` cannot import;
- `libcublas.so.12` is absent;
- `libcudnn.so.8` is absent.

## GPU Smoke Contract

PowerShell script:

```text
services/live-stt-service/scripts/gpu-smoke.ps1
```

The script:

1. optionally builds `Dockerfile.gpu`;
2. runs `nvidia-smi` inside the container;
3. checks `ctranslate2.get_cuda_device_count() > 0`;
4. prints supported CUDA compute types;
5. verifies cuBLAS and cuDNN linker entries;
6. reports optional FFmpeg CUDA, CUVID/NVDEC and NVENC capability;
7. starts the actual FastAPI service with `--gpus all`;
8. submits a real Turkish Common Voice WAV to `/transcribe`;
9. requires non-empty text and exact CUDA/compute metadata;
10. reports inference latency, wall time, image size, UID/GID and active GPU memory;
11. removes the temporary container in a `finally` block.

## Validation Completed on Laptop

| Check | Result |
|---|---|
| PowerShell parser | PASS |
| New GPU artifact unit tests | `3 passed` |
| Full non-integration unit suite | `52 passed` |
| Existing expected PII cases | `4 xfailed`, tracked by issue #97 |
| Existing PII cases unexpectedly passing | `7 xpassed` |
| Integration tests excluded by default | `3 deselected` |
| Coverage | `89%`, 465 statements |
| Mypy strict | PASS, 13 source files |
| Pip dependency check | No broken requirements |
| New #41 test Ruff | PASS |
| New #41 test Black | PASS |

Full-repository Ruff and Black remain affected by pre-existing source debt:

- Turkish dotted/dotless-I ambiguity warnings in existing files;
- existing import ordering in `app/main.py`;
- six pre-existing files not formatted by Black.

Those unrelated files were not mechanically rewritten in #41.

Bandit reports two existing low-severity `assert` uses in
`app/services/worker.py`. No medium or high severity finding was reported.

## Laptop Docker Attempt

A laptop build was started only to validate the image dependency graph. Docker
resolved the pinned NVIDIA tag and digest, then began downloading the official
CUDA/cuDNN base layers.

The base image contains layers of approximately 710 MB and 1.27 GB. During
download, Docker Engine disconnected with:

```text
failed to receive status:
rpc error: code = Unavailable
error reading from server: EOF
```

Docker Desktop then failed to restart. This is not recorded as an image PASS or
as a GPU failure. No `teas-*` container was modified, restarted or used by the
#41 test.

Large CUDA build and runtime validation are intentionally moved to the RTX 4070
GPU PC.

## RTX 4070 Evidence — RECORDED 2026-06-08

Executed on the clean test clone `C:\Users\denetimpc\platform-ai-final-stt-test`
(RTX 4070 Laptop GPU, driver 595.79). `gpu-smoke.ps1 -Model medium
-ComputeType float16` returned `GPU smoke PASS`.

| Evidence | Required | Recorded result |
|---|---|---|
| Docker build | PASS | PASS (289s, 14/14 layers) |
| Container GPU visibility | RTX 4070 | `NVIDIA GeForce RTX 4070 Laptop GPU, 595.79, 8188 MiB` |
| CTranslate2 CUDA devices | `>= 1` | `1` |
| Compute types | includes `float16` | `float16` present (also `int8_float16` advertised) |
| cuBLAS | `libcublas.so.12` | present at `/usr/local/cuda/.../libcublas.so.12` |
| cuDNN | `libcudnn.so.8` | present at `/usr/lib/x86_64-linux-gnu/libcudnn.so.8` |
| FFmpeg GPU | optional | `cuda` hwaccel + CUVID decoders + NVENC encoders detected |
| Real Turkish transcribe | non-empty | `"Geçiş ülkelerinde yaşananlar ise karışık."` |
| Response metadata | `device=cuda` | `device=cuda`, `compute_type=float16`, `model=medium`, `language=tr` |
| Runtime user | `10001:10001` | `10001:10001` |
| Image size | measured | `2393742183` bytes (~2.23 GiB) |
| Inference latency | measured | `673 ms` (audio 5.52s; first-request wall 15.9s incl. cached model load) |
| GPU memory | measured | `[N/A]` — `--query-compute-apps` caught no active process post-inference (timing only; GPU confirmed working) |
| Temporary container | removed | removed by `finally` block |

### Script fixes required during RTX 4070 validation

Two PowerShell-specific defects in `gpu-smoke.ps1` were found and fixed (build
and image itself were correct from the first run):

1. `83814f9` — quoted the `--query-gpu`/`--format` args so PowerShell does not
   array-split comma-separated tokens before they reach `nvidia-smi`.
2. `f175ab1` — step [5/6] container run now splats an explicit arg array to the
   native `docker.exe`; the `Invoke-Docker` advanced function was parsing `-d`
   and `-e` as common parameters (`-Debug`, ambiguous `-ErrorAction`/`-ErrorVariable`).

First-run model download must be primed into the mounted HF cache before the
timed transcribe (otherwise the in-request download exceeds the worker timeout
and returns 504). Pre-warm:

```powershell
docker run --rm --gpus all `
  --mount "type=bind,source=$env:USERPROFILE\.cache\huggingface,target=/home/stt/.cache/huggingface" `
  --entrypoint python3 live-stt-service:gpu-issue-41 `
  -c "from faster_whisper import WhisperModel; WhisperModel('medium', device='cuda', compute_type='float16')"
```

### Not yet executed (optional, non-blocking for #41 core)

- `int8_float16` second smoke run (compute mode advertised and parameterized;
  quality/latency comparison is #43 scope).
- Active GPU-memory figure (measurement timing gap only).

## GPU PC Commands

Use the clean test clone, not the approved dirty live-STT PoC directory:

```powershell
cd C:\Users\denetimpc\platform-ai-final-stt-test
# The clone is --single-branch, so a bare `git fetch origin` does not create the
# remote-tracking ref. Fetch the branch explicitly and check out FETCH_HEAD.
git fetch origin feature/pr-gpu-01-dockerfile-gpu
git checkout -B feature/pr-gpu-01-dockerfile-gpu FETCH_HEAD
cd services\live-stt-service
```

Run float16:

```powershell
.\scripts\gpu-smoke.ps1 `
  -Image live-stt-service:gpu-issue-41 `
  -Model medium `
  -ComputeType float16
```

Then run the second compute mode without rebuilding:

```powershell
.\scripts\gpu-smoke.ps1 `
  -SkipBuild `
  -Image live-stt-service:gpu-issue-41 `
  -Model medium `
  -ComputeType int8_float16 `
  -Port 18221
```

## Scope Boundaries

- No model production lock is made.
- No multi-worker concurrency is implemented; that is issue #42.
- No performance winner is selected; that is issue #43.
- NVDEC/NVENC are optional and not required for audio-only STT.
- No Workcube meeting recording is used.
- No production deployment or GitOps change is included.

## Risks

| Risk | Impact | Control |
|---|---|---|
| NVIDIA base image is large | Slow pull/build and disk pressure | Build on GPU host; retain Docker cache |
| Host driver/container mismatch | CUDA initialization failure | Fail-fast `nvidia-smi` and CTranslate2 probe |
| 8 GB VRAM | Limited parallel streams | Measure in #42 |
| Model cache mount permissions | Model download failure | Non-root writable cache mount; smoke fails visibly |
| NVDEC/NVENC unavailable | No video acceleration | Optional only; audio STT remains valid |
| Full repo lint debt | CI noise unrelated to #41 | Record existing debt; keep #41 files clean |

## Completion State

Implementation, laptop static validation, and RTX 4070 real-GPU validation are
complete. `gpu-smoke.ps1` returns `GPU smoke PASS` end-to-end: Docker build,
GPU visibility, CTranslate2 CUDA, cuBLAS/cuDNN linkage, real Turkish
transcription (`device=cuda`, `medium`/`float16`, 673 ms), non-root
`10001:10001`, and image size 2.23 GiB are all recorded above.

Issue #41 core scope (NVIDIA CUDA + cuDNN + faster-whisper CUDA image with real
GPU transcription) is validated. No model lock (#39), no multi-worker (#42), no
performance winner (#43), and no production/GitOps change were made.

Optional follow-ups, non-blocking: `int8_float16` second smoke run and an active
GPU-memory figure. AG-019 staging resource gate pending; implementation is
development-only.
