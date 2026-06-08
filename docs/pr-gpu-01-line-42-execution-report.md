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
| 0 | Architecture + CTranslate2 concurrency research | DONE |
| 1 | Saturation measurement harness + unit tests (laptop) | DONE (13/13 tests) |
| 2 | Real RTX 4070 measurement (1..N workers) | DONE (K=1..4, saturation found) |
| 3 | "CUDA stream per worker" source + experimental evidence | DONE (process-context, honest) |
| 4 | VRAM guard/config — measurement justifies it | ADDED (opt-in, default off; 6 tests) |
| 5 | Optional busy-worker observability (not an issue AC) | OPTIONAL |
| 6 | Final report with measured saturation point | DONE (this report) |

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

## Phase 2 — RTX 4070 Measurement Results (2026-06-08)

Harness: `gpu-saturation.ps1 -Workers 1,2,3,4 -Concurrency 0` on RTX 4070 Laptop
GPU (8188 MiB), image `live-stt-service:gpu-issue-41`, `medium`/`float16`,
fixture `sample-tr-cv17-001.wav` (5.52 s). Concurrency equals worker count K.

| K | Concurrency | Ok | Errors | p50 ms | p95 ms | Throughput rps | Overlap | Max conc. | VRAM base MiB |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0 | 507.4 | 507.4 | 1.97 | false | 1 | 2097 |
| 2 | 2 | 2 | 0 | 574.0 | 630.3 | 3.14 | true | 2 | 4194 |
| 3 | 3 | 3 | 0 | 925.7 | 935.7 | 3.20 | true | 3 | 6291 |
| 4 | 4 | 0 | 4 | — | — | 0.0 | false | 0 | 7835 |

### Findings

1. **Per-worker VRAM ≈ 2.05 GiB, linear** (2097 → 4194 → 6291 → 7835 MiB).
   Each worker holds a **full medium/float16 model copy** — weights are NOT
   shared. This confirms Phase 0.3 interpretation **A** (process-per-worker).
   The measured **memory budget per stream ≈ 2.05 GiB**.
2. **Concurrent inference is real**: `overlap=true` and `max_concurrency=K` at
   K=2 and K=3 — multiple inferences run simultaneously on one GPU.
3. **Throughput saturates at K≈2–3**: 1.97 → 3.14 → 3.20 rps. The 3rd worker
   adds VRAM but almost no throughput while p50 degrades 574 → 926 ms. GPU
   compute is the bottleneck beyond ~2 concurrent streams.
4. **K=4 = VRAM ceiling**: base VRAM already 7835 / 8188 MiB and all 4 requests
   failed (0 ok). Four full-model copies do not fit in 8 GiB.

### Saturation point

- Throughput knee: **K ≈ 2** (best rps/latency balance: ~3.1 rps, p50 574 ms).
- Hard VRAM ceiling: **K = 4 fails**; **K = 3 is the largest that fits**
  (6291 MiB) but with degraded latency and no throughput gain.
- Practical recommendation for the **current architecture** on RTX 4070 8 GiB:
  **2 workers (throughput-optimal), 3 maximum (VRAM-safe)**.

## Phase 3 — "CUDA stream per worker" evidence

- **Concurrency proven (experiment):** `overlap=true` with `max_concurrency=K`
  at K=2,3 demonstrates simultaneous GPU inference.
- **Mechanism (honest):** this is achieved by **separate worker processes =
  separate CUDA contexts**, *not* by CTranslate2 `inter_threads` CUDA streams.
  Signature evidence: per-worker VRAM scales linearly with the full model size
  (no weight sharing), which is characteristic of independent contexts rather
  than shared-weight multi-stream execution.
- **Implication:** the CTranslate2 native path (`num_workers`/`inter_threads`
  with shared weights, source: CTranslate2 parallel docs) would fit **more**
  concurrent streams in the same 8 GiB because only activation memory scales per
  stream. That is the route to raise the ceiling beyond 3 — a candidate for a
  follow-up, not required to close #42's measurement.

## Phase 4 — VRAM budget guard (ADDED 2026-06-08)

> **Transparency note.** This guard was **not** in the original codebase. It was
> added during #42 *after* the measurement, because Phase 2 showed that
> `STT_WORKER_MAX_WORKERS=4` silently collapses (0/4 ok, VRAM 7835/8188 MiB).
> It is a **safety rail**, not a throughput improvement (raising the ceiling is
> the separate CTranslate2 shared-weights follow-up in Phase 3).

### Why it was added

- Measured per-worker ≈ **2097 MiB**; device total 8188 MiB.
- Without a guard, an over-large worker count crashes every request.
- A guard capping `effective = min(requested, budget // per_worker)` turns the
  crash into a safe clamp + a warning log.

### What changed

| File | Change |
|---|---|
| `app/core/config.py` | `worker_vram_budget_mb` + `worker_vram_per_worker_mb` settings; `WorkerCountPlan` + `resolve_worker_count()` |
| `app/services/worker.py` | `ProcessWorkerPool.__init__` uses `resolve_worker_count`; logs a warning when clamped |
| `tests/unit/test_worker_vram_budget.py` | 6 GPU-free unit tests |

### Default behaviour is UNCHANGED (important)

- `STT_WORKER_VRAM_BUDGET_MB` default is **0 = disabled**. The guard never
  engages unless it is set **and** `STT_DEVICE=cuda`.
- CPU runs and the existing default config behave exactly as before.
- The per-worker figure (`STT_WORKER_VRAM_PER_WORKER_MB`, default 2100) is the
  **measured** value, never an auto-guess.

### How to enable (GPU, opt-in)

```text
STT_DEVICE=cuda
STT_WORKER_MAX_WORKERS=4
STT_WORKER_VRAM_BUDGET_MB=6300        # usable VRAM budget
STT_WORKER_VRAM_PER_WORKER_MB=2100    # measured medium/fp16
# -> effective workers = min(4, 6300 // 2100) = 3, with a warning log
```

### How to REMOVE it (if undesired)

Cleanest: `git revert` the guard commit (the one titled
`feat(live-stt): #42 add opt-in GPU VRAM admission guard`).

Manual alternative:

1. `app/services/worker.py` — restore `range(settings.worker_max_workers)` in
   `ProcessWorkerPool.__init__` and drop the `resolve_worker_count` import +
   clamp warning.
2. `app/core/config.py` — delete the two `worker_vram_*` fields, the
   `WorkerCountPlan` dataclass and `resolve_worker_count()`.
3. Delete `tests/unit/test_worker_vram_budget.py`.

Because the guard is default-off, leaving it in place has no runtime effect
until an operator opts in.

## Scope Boundaries

- No model production lock (#39).
- No performance winner selection (#43); #42 measures saturation, #43 decides.
- No production/GitOps change.
- VRAM guard/config only if Phase 2 data shows it is needed.
- Busy-worker gauge is not an issue AC; proposed only if it fits `metrics.py`.
