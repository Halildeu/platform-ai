# #42 Shared-model multi-stream — RTX 4070 measurement

**Date:** 2026-06-09
**Target:** RTX 4070, faster-whisper 1.0.3 / CTranslate2, model `medium`, `compute_type=float16`
**Harness:** `scripts/measure_shared.py <backend> <K> <N>` (peak VRAM sampled via `nvidia-smi` @10 Hz; audio = `tests/fixtures/wer-cv17-tr/sample-tr-cv17-001.wav`)
**Backends:** `shared` (this change — one supervised process, single `WhisperModel(num_workers=K)`) vs `process` (per-worker model, the #20 pool).

## Results

| Backend | K | N | Peak VRAM | p50 ms | p95 ms | Throughput | Outcome |
|---------|---|---|-----------|--------|--------|------------|---------|
| shared  | 1 | 1 | 2275 MB   | 564    | 564    | 1.77 req/s | OK |
| shared  | 2 | 2 | 2475 MB   | 653    | 667    | **3.00 req/s** | OK (peak throughput) |
| shared  | 4 | 4 | 3003 MB   | 3421   | 3523   | 1.13 req/s | OK (compute-saturated) |
| process | 4 | 4 | ~7819 MB  | —      | —      | —          | **FAIL** — GPU low-memory warning → `WorkerTimeoutError` |

## Findings

1. **VRAM is ~flat under the shared backend** (2275 → 3003 MB across 1→4 concurrent
   streams, +32%), because the model weights are loaded once. The per-worker
   `process` pool instead grows ~linearly and reached ~7819 MB at K=4, where the
   GPU raised a low-memory warning and the run collapsed into a worker timeout —
   reproducing the previously recorded "K=4 all-fail" result.
   → This is the core #42 deliverable: shared weights + memory budget per stream.

2. **Concurrency buys capacity, not raw speed, on a single GPU.** Throughput peaks
   at K=2 (3.00 req/s) where two CUDA streams overlap well, then degrades by K=4
   (1.13 req/s) as the single GPU's compute saturates and per-request latency rises
   (564 → 3421 ms p50). **Saturation point ≈ K=2–3** for medium/fp16 on this card.
   The win of the shared backend is serving more concurrent sessions without OOM,
   not lower latency per request.

## Recommendation (feeds #43)

- For RTX 4070 / medium-fp16, run the shared backend with **K=2** as the
  throughput sweet spot; K=3 only if latency budget allows.
- The per-stream VRAM admission guard (`STT_WORKER_VRAM_BUDGET_MB` /
  `STT_WORKER_VRAM_PER_WORKER_MB`) should use the *marginal* per-stream figure
  measured here (~100–250 MB), not a full model copy, in shared mode.
- `process` backend remains the CPU / strong-isolation default; `shared` is the
  GPU concurrency path.

## Reproduce

```bash
cd services/live-stt-service
export PYTHONPATH=.
python scripts/measure_shared.py shared 1 1
python scripts/measure_shared.py shared 2 2
python scripts/measure_shared.py shared 4 4
python scripts/measure_shared.py process 4 4   # per-worker baseline
```
