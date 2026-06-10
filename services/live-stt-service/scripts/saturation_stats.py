"""Pure aggregation helpers for the #42 GPU saturation harness.

No I/O, no GPU, no third-party deps — so the maths is unit-testable on the
laptop while the actual load run happens on the RTX 4070 PC.

A "request record" is a dict with at least:
    start:      float  monotonic start (seconds)
    end:        float  monotonic end (seconds)
    elapsed_ms: float  per-request wall time
    ok:         bool   request succeeded (HTTP 200)
    status:     int    HTTP status (-1 on transport error)
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile. `pct` in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0.0 <= pct <= 100.0:
        raise ValueError("pct must be within [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


def throughput_rps(completed: int, wall_seconds: float) -> float:
    """Completed requests per wall-clock second."""
    if wall_seconds <= 0:
        return 0.0
    return completed / wall_seconds


def any_overlap(intervals: Sequence[tuple[float, float]]) -> bool:
    """True if any two [start, end] intervals overlap.

    Overlap of two in-flight requests is the observable signal that inference
    ran concurrently (rather than being serialized by the pool/driver).
    """
    ordered = sorted(intervals, key=lambda iv: iv[0])
    # strict overlap: a request started before the previous one finished
    return any(curr[0] < prev[1] for prev, curr in pairwise(ordered))


def max_concurrency(intervals: Sequence[tuple[float, float]]) -> int:
    """Maximum number of requests in flight at the same instant."""
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    # process ends before starts at an identical timestamp to avoid over-count
    events.sort(key=lambda e: (e[0], e[1]))
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def summarize(records: Sequence[dict[str, object]]) -> dict[str, object]:
    """Aggregate per-request records into one saturation data point."""
    n_total = len(records)
    ok = [r for r in records if r.get("ok")]
    err = [r for r in records if not r.get("ok")]
    latencies = [float(r["elapsed_ms"]) for r in ok]  # type: ignore[arg-type]

    summary: dict[str, object] = {
        "requests": n_total,
        "ok": len(ok),
        "errors": len(err),
        "status_counts": _status_counts(records),
    }

    if latencies:
        summary["p50_ms"] = round(percentile(latencies, 50), 1)
        summary["p95_ms"] = round(percentile(latencies, 95), 1)
        summary["max_ms"] = round(max(latencies), 1)
    else:
        summary["p50_ms"] = summary["p95_ms"] = summary["max_ms"] = None

    starts = [float(r["start"]) for r in ok]  # type: ignore[arg-type]
    ends = [float(r["end"]) for r in ok]  # type: ignore[arg-type]
    if starts and ends:
        wall = max(ends) - min(starts)
        summary["wall_s"] = round(wall, 3)
        summary["throughput_rps"] = round(throughput_rps(len(ok), wall), 3)
        intervals = list(zip(starts, ends, strict=True))
        summary["overlap"] = any_overlap(intervals)
        summary["max_concurrency"] = max_concurrency(intervals)
    else:
        summary["wall_s"] = None
        summary["throughput_rps"] = 0.0
        summary["overlap"] = False
        summary["max_concurrency"] = 0

    return summary


def _status_counts(records: Sequence[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        key = str(r.get("status", "?"))
        counts[key] = counts.get(key, 0) + 1
    return counts
