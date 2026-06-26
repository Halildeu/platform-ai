"""Tests for the Faz 24 T-C G-INT quality gate verifier."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import gint_gate  # noqa: E402


def _sha(char: str) -> str:
    return "sha256:" + (char * 64)


def _sample_count_hash(eval_set_hash: str, n_samples: int) -> str:
    raw = f"{eval_set_hash}\n{n_samples}\n"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pilot_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "tag": "ollama-pilot",
        "backend": "ollama",
        "model": "llama3.1:8b",
        "dataset_kind": "pilot-meeting",
        "eval_set": "C:/faz24-pilot/intel-pilot-2026-06-25.json",
        "eval_set_hash": _sha("a"),
        "prompt_hash": _sha("b"),
        "sample_manifest_hash": _sha("c"),
        "n_samples": 8,
        "grounding_rate": 1.0,
        "action_precision": 0.86,
        "action_recall": 0.83,
        "action_f1": 0.845,
        "decision_precision": 0.82,
        "decision_recall": 0.80,
        "decision_f1": 0.81,
        "schema_invalid_rate": 0.0,
        "format_invalid_rate": 0.0,
        "backend_error_rate": 0.0,
        "truncation_risk_rate": 0.0,
        "p50_ms": 12516,
        "ollama_options": {"temperature": 0.0, "num_ctx": 8192, "top_p": 0.9},
        "keep_alive": "0",
    }
    row.update(overrides)
    if "sample_count_hash" not in overrides:
        row["sample_count_hash"] = _sample_count_hash(
            str(row["eval_set_hash"]), int(row["n_samples"])
        )
    return row


def _thresholds() -> dict[str, object]:
    return {
        "min_grounding_rate": 0.95,
        "min_action_precision": 0.80,
        "min_action_recall": 0.80,
        "min_decision_precision": 0.75,
        "min_decision_recall": 0.75,
        "max_schema_invalid_rate": 0.0,
        "max_format_invalid_rate": 0.0,
        "max_backend_error_rate": 0.0,
        "max_truncation_risk_rate": 0.0,
        "min_samples": 3,
    }


def _evaluate(rows: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    params = _thresholds()
    params.update(overrides)
    return gint_gate.evaluate_gate(rows=rows, **params)  # type: ignore[arg-type]


def test_gate_passes_with_pilot_gint_under_thresholds() -> None:
    result = _evaluate([_pilot_row()])

    assert result["status"] == "pass"
    assert result["findingCount"] == 0
    assert result["selectedGint"]["kind"] == "pilot-meeting"
    assert result["selectedGint"]["grounding_rate"] == 1.0
    assert result["selectedGint"]["eval_set_hash"] == _sha("a")
    assert result["selectedGint"]["prompt_hash"] == _sha("b")
    assert result["selectedGint"]["sample_manifest_hash"] == _sha("c")
    assert result["selectedGint"]["sample_count_hash"] == _sample_count_hash(
        _sha("a"), 8
    )


def test_synthetic_rows_do_not_satisfy_pilot_gate() -> None:
    result = _evaluate(
        [
            {
                "tag": "ollama-llama31",
                "backend": "ollama",
                "model": "llama3.1:8b",
                "eval_set": "tests/fixtures/intel-eval.json",
                "eval_set_hash": "fixturehash",
                "prompt_hash": "prompthash",
                "n_samples": 8,
                "grounding_rate": 1.0,
                "action_precision": 1.0,
                "action_recall": 1.0,
                "decision_precision": 1.0,
                "decision_recall": 1.0,
                "schema_invalid_rate": 0.0,
                "format_invalid_rate": 0.0,
                "backend_error_rate": 0.0,
                "truncation_risk_rate": 0.0,
            }
        ]
    )

    assert result["status"] == "blocked"
    assert "synthetic-neutral" in result["kinds"]
    assert any(
        "no pilot-meeting G-INT row" in finding for finding in result["findings"]
    )


def test_pilot_tag_without_explicit_dataset_kind_is_not_enough() -> None:
    row = _pilot_row(tag="ollama-pilot")
    row.pop("dataset_kind")

    result = _evaluate([row])

    assert result["status"] == "blocked"
    assert "unknown" in result["kinds"]
    assert result["selectedGint"] is None


def test_mock_backend_cannot_be_spoofed_as_pilot() -> None:
    result = _evaluate([_pilot_row(backend="mock")])

    assert result["status"] == "blocked"
    assert any("backend=mock" in finding for finding in result["findings"])
    assert result["selectedGint"] is None


def test_missing_backend_cannot_be_spoofed_as_pilot() -> None:
    row = _pilot_row()
    row.pop("backend")

    result = _evaluate([row])

    assert result["status"] == "blocked"
    assert any("backend=<missing>" in finding for finding in result["findings"])
    assert result["selectedGint"] is None


def test_pilot_hashes_must_be_full_sha256_values() -> None:
    result = _evaluate(
        [
            _pilot_row(
                eval_set_hash="abc123def456",
                prompt_hash="sha256:not-a-real-digest",
                sample_manifest_hash="not-a-real-digest",
                sample_count_hash="also-not-a-real-digest",
            )
        ]
    )

    assert result["status"] == "blocked"
    assert any(
        "pilot row 1 eval_set_hash must be sha256:<64 hex>" in finding
        for finding in result["findings"]
    )
    assert any(
        "pilot row 1 prompt_hash must be sha256:<64 hex>" in finding
        for finding in result["findings"]
    )
    assert any(
        "pilot row 1 sample_manifest_hash must be sha256:<64 hex>" in finding
        for finding in result["findings"]
    )
    assert any(
        "pilot row 1 sample_count_hash must be sha256:<64 hex>" in finding
        for finding in result["findings"]
    )
    assert result["selectedGint"] is None


def test_pilot_sample_count_hash_must_match_eval_set_hash_and_count() -> None:
    result = _evaluate([_pilot_row(sample_count_hash=_sha("f"))])

    assert result["status"] == "blocked"
    assert any(
        "sample_count_hash does not match eval_set_hash+n_samples" in finding
        for finding in result["findings"]
    )
    assert result["selectedGint"] is None


def test_pilot_sample_count_must_be_positive_integer() -> None:
    result = _evaluate([_pilot_row(n_samples=8.5, sample_count_hash=_sha("f"))])

    assert result["status"] == "blocked"
    assert any(
        "n_samples must be a positive integer" in finding
        for finding in result["findings"]
    )
    assert result["selectedGint"] is None


def test_fixture_path_cannot_be_spoofed_as_pilot() -> None:
    result = _evaluate([_pilot_row(eval_set="tests/fixtures/intel-pilot.json")])

    assert result["status"] == "blocked"
    assert any("tests/fixtures" in finding for finding in result["findings"])
    assert result["selectedGint"] is None


def test_thresholds_are_required() -> None:
    result = _evaluate([_pilot_row()], min_grounding_rate=None)

    assert result["status"] == "blocked"
    assert any(
        "explicit G-INT thresholds are required" in f for f in result["findings"]
    )


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("grounding_rate", 0.90),
        ("action_precision", 0.70),
        ("action_recall", 0.70),
        ("decision_precision", 0.70),
        ("decision_recall", 0.70),
        ("schema_invalid_rate", 0.10),
        ("format_invalid_rate", 0.10),
        ("backend_error_rate", 0.10),
        ("truncation_risk_rate", 0.10),
        ("n_samples", 1),
    ],
)
def test_gate_fails_when_pilot_metric_misses_threshold(
    metric: str, value: float
) -> None:
    result = _evaluate([_pilot_row(**{metric: value})])

    assert result["status"] == "fail"
    assert result["findingCount"] >= 1


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "transcript",
        "expected_actions",
        "expected_decisions",
        "prompt",
        "response",
        "citations",
        "attendees",
        "participants",
        "speaker_name",
        "iban",
    ],
)
def test_gate_rejects_raw_content_fields(unsafe_key: str) -> None:
    row = _pilot_row()
    row[unsafe_key] = "raw meeting content"

    result = _evaluate([row])

    assert result["status"] == "fail"
    assert unsafe_key in result["findings"][0]


def test_gate_rejects_nested_raw_content_or_pii() -> None:
    row = _pilot_row()
    row["debug"] = {"samples": [{"transcript": "ali@example.com karar verdi"}]}

    result = _evaluate([row])

    assert result["status"] == "fail"
    assert any("debug.samples" in finding for finding in result["findings"])
    assert all("ali@example.com" not in finding for finding in result["findings"])


def test_gate_rejects_nested_iban_without_echoing_value() -> None:
    row = _pilot_row()
    row["debug"] = {"payment_ref": "TR330006100519786457841326"}

    result = _evaluate([row])

    assert result["status"] == "fail"
    assert any("PII-shaped" in finding for finding in result["findings"])
    assert all(
        "TR330006100519786457841326" not in finding for finding in result["findings"]
    )


def test_gate_ignores_multiseed_aggregate_rows_when_pilot_row_passes() -> None:
    aggregate = {
        "summary": "per-model mean±stdev across seeds",
        "model": "llama3.1:8b",
        "grounding_rate_mean": 0.97,
        "action_precision_mean": 0.85,
    }

    result = _evaluate([_pilot_row(), aggregate])

    assert result["status"] == "pass"


def test_load_rows_supports_jsonl_and_object_with_rows(tmp_path: Path) -> None:
    jsonl = tmp_path / "gint.jsonl"
    jsonl.write_text(
        json.dumps(_pilot_row(), ensure_ascii=False)
        + "\n"
        + json.dumps(_pilot_row(tag="pilot-2"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    assert len(gint_gate._load_rows(jsonl)) == 2

    wrapped = tmp_path / "gint-wrapped.json"
    wrapped.write_text(json.dumps({"rows": [_pilot_row()]}), encoding="utf-8")
    assert len(gint_gate._load_rows(wrapped)) == 1
