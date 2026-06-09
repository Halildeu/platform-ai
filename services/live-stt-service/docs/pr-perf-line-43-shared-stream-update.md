# #43 Post-GPU performance matrix — shared-stream update

**Date:** 2026-06-09
**Source:** `#42` shared-model multi-stream measurement on RTX 4070 (medium / float16).
See `docs/gpu-42-shared-stream-measurement.md` for the raw harness output.

This update feeds the #43 matrix with the GPU concurrency numbers now that the
shared-model backend exists. WER and the final ₺/hour cost line remain carried
over from the WER pilot (#33–37) and the hardware decision (#40) and are not
re-measured here.

## Performance / memory matrix (live-stt, medium, float16, RTX 4070)

| Mode            | Streams (K) | Peak VRAM | Latency p50 | Latency p95 | Throughput | Outcome |
|-----------------|-------------|-----------|-------------|-------------|------------|---------|
| shared          | 1           | 2275 MB   | 564 ms      | 564 ms      | 1.77 req/s | OK |
| shared          | 2           | 2475 MB   | 653 ms      | 667 ms      | 3.00 req/s | OK (peak throughput) |
| shared          | 4           | 3003 MB   | 3421 ms     | 3523 ms     | 1.13 req/s | OK (compute-saturated) |
| process (old)   | 4           | ~7819 MB  | —           | —           | —          | FAIL (GPU low-mem → timeout) |

### Latency / audio-minute
The fixture is a short Common Voice TR clip; per-minute figures should be taken
from the WER pilot recordings once available. Relative behaviour holds: latency
is flat up to K=2, then climbs sharply as the single GPU saturates.

### Peak VRAM
Shared mode loads weights once → near-flat (2.3→3.0 GB for 1→4 streams).
Per-worker process mode grows ~linearly → ~7.8 GB at K=4 and collapses. This is
the decisive operational difference for capacity planning.

### Cost (₺/hour) — carried over, not re-measured
Use the #40 hardware decision basis (electricity + amortisation for owned RTX
4070, or cloud GPU hourly). The throughput numbers above let cost/audio-hour be
derived once the production K is fixed.

## Production recommendation

- **Backend:** `shared` for GPU deployment; `process` stays the CPU / strong
  isolation default.
- **Concurrency:** K=2 is the throughput sweet spot on RTX 4070 / medium-fp16;
  K=3 only if the latency budget allows. K≥4 is compute-bound, not memory-bound.
- **Capacity, not speed:** multi-stream raises concurrent-session capacity at a
  fixed VRAM budget; it does not lower single-request latency on one GPU.
- **Open for #43 closure:** plug in WER (model choice, #ADR-0031) and the final
  ₺/hour line, then record the production go/no-go.
