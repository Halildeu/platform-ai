# PR-gpu-01 Line #42 Execution Report

Issue: `#42 [PR-gpu-01] Multi-worker GPU stream parallelism`

Canonical issue body (read 2026-06-08 via `gh issue view 42`):

> Aynı GPU üzerinde multi-stream:
> - CUDA stream per worker
> - Concurrent inference (faster-whisper batch veya multi-stream)
> - Memory budget per stream
> - Saturation point ölçüm

This report is **measurement-first**. No automatic worker clamping and no
hard-coded VRAM estimates are introduced before real RTX 4070 numbers exist.

## Status

| Phase | Description | State |
|---|---|---|
| 0 | Architecture + CTranslate2 concurrency research | IN PROGRESS |
| 1 | Saturation measurement harness + unit tests (laptop) | PENDING |
| 2 | Real RTX 4070 measurement (1..N workers) | PENDING |
| 3 | "CUDA stream per worker" source + experimental evidence | PENDING |
| 4 | VRAM guard/config — only if measurement requires it | CONDITIONAL |
| 5 | Optional busy-worker observability (not an issue AC) | OPTIONAL |
| 6 | Final report with measured saturation point | PENDING |

---

## Phase 0 — Current Architecture vs CTranslate2 Concurrency Model

### 0.1 What the service does today

`app/services/worker.py` `ProcessWorkerPool` (from PR-stt-03 / #20):

- Spawns `STT_WORKER_MAX_WORKERS` separate `multiprocessing` (`spawn`) processes.
- **Each process independently loads its own `WhisperModel(...)`** (lazy, on first job).
- A `BoundedSemaphore` + round-robin assigns one in-flight job per worker slot.
- Crash → respawn; per-request timeout → terminate/kill + respawn.

Consequence on GPU:

- Each worker process creates its **own CUDA context** on device 0.
- **Model weights are NOT shared** — every worker holds a full copy of the
  Whisper weights in VRAM.
- There is **no explicit CUDA stream management** and no `device_index` /
  `num_workers` argument passed to CTranslate2.

> Therefore "CUDA stream per worker" is **not** demonstrated by the current code.
> Separate processes give separate CUDA *contexts* (time-sliced, or MPS if
> enabled), which is a different mechanism from CTranslate2 CUDA *streams*.

### 0.2 How CTranslate2 / faster-whisper actually parallelize

Source: CTranslate2 "Parallel and asynchronous execution" docs.

- `inter_threads` (exposed by faster-whisper as `num_workers`) sets the number
  of parallel workers **inside a single model instance**. On one GPU, each such
  worker is assigned **its own CUDA stream**, so multiple batches run
  concurrently via stream-level scheduling.
- `device_index=[...]` pins workers to GPUs (list → multi-GPU). A single GPU
  uses one index with multiple `inter_threads`.
- `intra_threads` = threads within an op (matmul/softmax); set to 1 under data
  parallelism to avoid contention.
- **Model weights are shared** when workers run on the same device — "the model
  weights are shared to save on memory."
- Concurrency is triggered by calling `transcribe` from multiple Python threads,
  or `asynchronous=True`, or `max_batch_size` sub-batching.

### 0.3 The two interpretations of "multi-worker" (to be measured)

| | A. Current: process-per-worker | B. CTranslate2 stream-per-worker |
|---|---|---|
| Mechanism | N OS processes, N CUDA contexts | 1 process, `num_workers=N`, N CUDA streams |
| Model weights | full copy per worker | **shared** |
| Expected VRAM | ~N × (weights + activations) | weights + N × activations |
| Matches issue wording | "concurrent inference" only | "CUDA stream per worker" + "memory budget per stream" |
| Code change needed | none (already exists) | pass `num_workers`/threads + threaded dispatch |

Interpretation **B** is what the issue's "CUDA stream per worker" and "memory
budget per stream" wording points to, and it is far more VRAM-efficient. But the
decision is **deferred to measurement** (Phase 2): we first baseline the current
process-parallel behavior on the real RTX 4070, capture per-worker VRAM and
throughput, then decide whether B is required.

### 0.4 Open questions for the experiment (Phase 3)

- Does running N worker *processes* actually overlap inference on the GPU, or
  does the driver serialize the contexts? (timestamp overlap + `nvidia-smi`
  concurrent-process evidence)
- Measured VRAM delta per added worker (process model) vs shared-weight model.
- Where is the throughput saturation knee on 8 GB / RTX 4070 Laptop?

---

## Phase 1 — Measurement Harness (design)

Goal: a harness that, for a given worker count `K` and concurrency `C`, drives
the GPU container and records **throughput, p50/p95 latency, per-run VRAM, and
error/OOM counts** — with no change to runtime defaults.

Planned artifacts:

- `scripts/gpu-saturation.ps1` — orchestrates: start container with
  `STT_WORKER_MAX_WORKERS=K`, sample `nvidia-smi` VRAM during load, fire `C`
  concurrent `/transcribe` requests, collect per-request latency + status,
  compute aggregate metrics, tear down. Sweeps `K = 1..N`.
- A small concurrent client (PowerShell jobs or a tiny Python script) to issue
  `C` simultaneous requests and emit per-request timing as JSON lines.
- `tests/unit/test_saturation_parse.py` — unit tests for the **pure**
  aggregation/parsing helpers (latency percentiles, throughput, overlap
  detection from timestamps). The real GPU run happens on the RTX 4070 PC.

No auto-clamp, no VRAM constant. Pure measurement.

---

## Phase 2 — RTX 4070 Measurement Results

_To be appended after running the harness on the GPU PC._

## Phase 3 — CUDA Stream Evidence

_To be appended: source citation + experimental overlap/VRAM evidence._

## Scope Boundaries

- No model production lock (#39).
- No performance winner selection (#43); #42 measures saturation, #43 decides.
- No production/GitOps change.
- VRAM guard/config only if Phase 2 data shows it is needed.
- Busy-worker gauge is not an issue AC; proposed only if it fits `metrics.py`.
