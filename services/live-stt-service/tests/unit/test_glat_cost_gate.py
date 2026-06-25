"""Tests for the Faz 24 G-LAT/COST quality gate verifier."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import glat_cost_gate  # noqa: E402


def _thresholds() -> dict[str, float | None]:
    return {
        "max_latency_p95_ms": 2500.0,
        "max_queue_lag_p95_ms": 500.0,
        "max_cost_per_audio_minute": 0.01,
        "max_realtime_factor": 0.35,
        "max_error_rate": 0.01,
        "min_audio_minutes": 30.0,
        "min_audio_minutes_per_wall_hour": 600.0,
    }


def _pilot_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "tag": "pilot-gpu-live-stt-k2",
        "dataset_kind": "pilot-meeting",
        "backend": "live-stt-service",
        "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "compute": "float16",
        "n_samples": 12,
        "audio_minutes": 48.0,
        "latency_p50_ms": 980.0,
        "latency_p95_ms": 1800.0,
        "queue_lag_p95_ms": 220.0,
        "realtime_factor": 0.08,
        "audio_minutes_per_wall_hour": 1500.0,
        "cost_per_audio_minute": 0.0012,
        "gpu_utilization_p95_pct": 76.0,
        "error_rate": 0.0,
        "evidence_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    row.update(overrides)
    return row


def test_gate_passes_with_complete_pilot_row_under_thresholds() -> None:
    result = glat_cost_gate.evaluate_gate(rows=[_pilot_row()], **_thresholds())

    assert result["status"] == "pass"
    assert result["findingCount"] == 0
    assert result["selectedGlatCost"]["latency_p95_ms"] == 1800.0
    assert result["selectedGlatCost"]["cost_per_audio_minute"] == 0.0012


def test_lab_or_synthetic_evidence_does_not_satisfy_pilot_gate() -> None:
    result = glat_cost_gate.evaluate_gate(
        rows=[
            {
                "tag": "legacy-gpu-matrix-2026-06-10",
                "dataset_kind": "perf-lab",
                "backend": "live-stt-service",
                "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
                "compute": "float16",
                "n_samples": 4,
                "audio_minutes": 3.5,
                "latency_p50_ms": 770.0,
                "latency_p95_ms": 1100.0,
                "queue_lag_p95_ms": 0.0,
                "realtime_factor": 0.06,
                "audio_minutes_per_wall_hour": 1700.0,
                "cost_per_audio_minute": 0.00095,
                "gpu_utilization_p95_pct": 71.0,
                "error_rate": 0.0,
                "evidence_hash": "sha256:" + ("b" * 64),
            }
        ],
        **_thresholds(),
    )

    assert result["status"] == "blocked"
    assert result["kinds"] == ["perf-lab"]
    assert any("no pilot-meeting G-LAT/COST row" in item for item in result["findings"])


def test_thresholds_are_required() -> None:
    thresholds = _thresholds()
    thresholds["max_cost_per_audio_minute"] = None

    result = glat_cost_gate.evaluate_gate(rows=[_pilot_row()], **thresholds)

    assert result["status"] == "blocked"
    assert any("explicit G-LAT/COST thresholds" in item for item in result["findings"])


def test_gate_fails_when_pilot_latency_exceeds_threshold() -> None:
    result = glat_cost_gate.evaluate_gate(
        rows=[_pilot_row(latency_p95_ms=3200.0)],
        **_thresholds(),
    )

    assert result["status"] == "fail"
    assert any("latency_p95_ms" in item for item in result["findings"])


def test_gate_fails_when_pilot_cost_exceeds_threshold() -> None:
    result = glat_cost_gate.evaluate_gate(
        rows=[_pilot_row(cost_per_audio_minute=0.05)],
        **_thresholds(),
    )

    assert result["status"] == "fail"
    assert any("cost_per_audio_minute" in item for item in result["findings"])


def test_incomplete_pilot_row_is_blocked() -> None:
    row = _pilot_row()
    del row["evidence_hash"]

    result = glat_cost_gate.evaluate_gate(rows=[row], **_thresholds())

    assert result["status"] == "blocked"
    assert any("evidence_hash" in item for item in result["findings"])


def test_pilot_evidence_hash_must_be_full_sha256() -> None:
    result = glat_cost_gate.evaluate_gate(
        rows=[_pilot_row(evidence_hash="sha256:not-a-real-digest")],
        **_thresholds(),
    )

    assert result["status"] == "blocked"
    assert any("sha256:<64 hex>" in item for item in result["findings"])


@pytest.mark.parametrize(
    ("unsafe_key", "unsafe_value"),
    [
        ("audio_path", "customer-call.wav"),
        ("transcript", "raw spoken text"),
        ("operator_note", "Call me at +905551112233"),
    ],
)
def test_gate_rejects_raw_audio_text_or_pii(unsafe_key: str, unsafe_value: str) -> None:
    row = _pilot_row()
    row[unsafe_key] = unsafe_value

    result = glat_cost_gate.evaluate_gate(rows=[row], **_thresholds())

    assert result["status"] == "fail"
    assert result["findingCount"] >= 1
    assert all("customer-call.wav" not in item for item in result["findings"])
    assert all("+905551112233" not in item for item in result["findings"])


def test_current_repo_snapshot_is_expected_blocked() -> None:
    evidence = (
        Path(__file__).resolve().parents[4] / "docs/evidence/latcost-results-2026-06-25.jsonl"
    )

    result = glat_cost_gate.evaluate_gate(rows=glat_cost_gate._load_rows(evidence), **_thresholds())

    assert result["status"] == "blocked"
    assert result["kinds"] == ["perf-lab"]


def test_load_rows_supports_jsonl_and_object_with_rows(tmp_path: Path) -> None:
    jsonl = tmp_path / "latcost.jsonl"
    jsonl.write_text(
        json.dumps(_pilot_row(), ensure_ascii=False)
        + "\n"
        + json.dumps(_pilot_row(latency_p95_ms=1700.0), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    assert len(glat_cost_gate._load_rows(jsonl)) == 2

    wrapped = tmp_path / "latcost.json"
    wrapped.write_text(json.dumps({"rows": [_pilot_row()]}), encoding="utf-8")
    assert len(glat_cost_gate._load_rows(wrapped)) == 1
