"""Unit tests for the #42 saturation pure stats helpers.

GPU-free: validates percentile, throughput, overlap and concurrency logic so
the measurement harness reports trustworthy numbers before any RTX 4070 run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# saturation_stats lives under scripts/, not the app package.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import saturation_stats as ss  # noqa: E402


def test_percentile_basic() -> None:
    values = [10, 20, 30, 40, 50]
    assert ss.percentile(values, 50) == 30
    assert ss.percentile(values, 0) == 10
    assert ss.percentile(values, 100) == 50


def test_percentile_interpolates() -> None:
    # p95 of 1..100 sits between 95 and 96
    values = list(range(1, 101))
    assert ss.percentile(values, 95) == pytest.approx(95.05, abs=0.01)


def test_percentile_single_value() -> None:
    assert ss.percentile([42.0], 95) == 42.0


def test_percentile_rejects_empty() -> None:
    with pytest.raises(ValueError):
        ss.percentile([], 50)


def test_percentile_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        ss.percentile([1.0], 150)


def test_throughput() -> None:
    assert ss.throughput_rps(10, 2.0) == 5.0
    assert ss.throughput_rps(5, 0.0) == 0.0


def test_any_overlap_true() -> None:
    # second request starts before first finishes
    assert ss.any_overlap([(0.0, 2.0), (1.0, 3.0)]) is True


def test_any_overlap_false_serial() -> None:
    assert ss.any_overlap([(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]) is False


def test_max_concurrency() -> None:
    intervals = [(0.0, 3.0), (1.0, 2.0), (1.5, 4.0)]
    # at t=1.5 all three overlap
    assert ss.max_concurrency(intervals) == 3


def test_max_concurrency_serial() -> None:
    intervals = [(0.0, 1.0), (1.0, 2.0)]
    assert ss.max_concurrency(intervals) == 1


def _rec(start: float, end: float, ok: bool = True, status: int = 200) -> dict:
    return {
        "start": start,
        "end": end,
        "elapsed_ms": (end - start) * 1000.0,
        "ok": ok,
        "status": status,
    }


def test_summarize_concurrent() -> None:
    records = [_rec(0.0, 2.0), _rec(0.1, 2.1), _rec(0.2, 2.2)]
    summary = ss.summarize(records)
    assert summary["requests"] == 3
    assert summary["ok"] == 3
    assert summary["errors"] == 0
    assert summary["overlap"] is True
    assert summary["max_concurrency"] == 3
    assert summary["throughput_rps"] > 0
    assert summary["p50_ms"] is not None


def test_summarize_with_errors() -> None:
    records = [_rec(0.0, 2.0), _rec(0.0, 0.5, ok=False, status=504)]
    summary = ss.summarize(records)
    assert summary["ok"] == 1
    assert summary["errors"] == 1
    assert summary["status_counts"]["504"] == 1


def test_summarize_all_failed() -> None:
    records = [_rec(0.0, 0.5, ok=False, status=503)]
    summary = ss.summarize(records)
    assert summary["ok"] == 0
    assert summary["p50_ms"] is None
    assert summary["throughput_rps"] == 0.0
    assert summary["overlap"] is False
