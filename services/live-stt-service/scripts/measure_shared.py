"""#42 GPU measurement harness for the shared-model multi-stream backend.

Runs N concurrent transcriptions through a chosen worker backend and reports
peak VRAM (sampled via nvidia-smi), wall time, throughput and p50/p95 latency.

Usage (from services/live-stt-service, with a CUDA-enabled venv):
    python scripts/measure_shared.py <backend> <K> [N] [audio_path]
      backend : shared | process | inline
      K       : worker / CUDA-stream count
      N       : concurrent requests (default = K)

Example sweep:
    python scripts/measure_shared.py shared 1 1
    python scripts/measure_shared.py shared 4 4
    python scripts/measure_shared.py process 4 4   # per-worker baseline (linear VRAM)
"""

from __future__ import annotations

import math
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.config import Settings
from app.services.worker import build_worker_pool

_DEFAULT_AUDIO = "tests/fixtures/wer-cv17-tr/sample-tr-cv17-001.wav"


def vram_used_mb() -> int:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    )
    return int(out.decode().splitlines()[0].strip())


def main() -> None:
    backend = sys.argv[1] if len(sys.argv) > 1 else "shared"
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    n = int(sys.argv[3]) if len(sys.argv) > 3 else k
    audio = sys.argv[4] if len(sys.argv) > 4 else _DEFAULT_AUDIO

    settings = Settings(
        worker_backend=backend,
        worker_max_workers=k,
        device="cuda",
        compute_type="float16",
        model_name="medium",
        request_timeout=300,
    )
    pool = build_worker_pool(settings)

    peak = {"mb": 0}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            try:
                peak["mb"] = max(peak["mb"], vram_used_mb())
            except Exception:  # noqa: BLE001 - sampling is best-effort
                pass
            time.sleep(0.1)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    pool.transcribe(audio, timeout_sec=300)  # warmup: load model weights

    def one(_: int) -> float:
        start = time.perf_counter()
        pool.transcribe(audio, timeout_sec=300)
        return (time.perf_counter() - start) * 1000

    run_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as executor:
        latencies = sorted(executor.map(one, range(n)))
    wall = time.perf_counter() - run_start

    stop.set()
    sampler_thread.join()

    p95 = latencies[min(len(latencies) - 1, math.ceil(0.95 * len(latencies)) - 1)]
    print(f"backend={backend} K={k} N={n}")
    print(f"peak_vram_mb={peak['mb']}")
    print(f"wall_s={wall:.2f} throughput_req_s={n / wall:.2f}")
    print(f"p50_ms={statistics.median(latencies):.0f} p95_ms={p95:.0f} max_ms={latencies[-1]:.0f}")

    pool.close()


if __name__ == "__main__":
    main()
