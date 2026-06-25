"""Tests for the Faz 24 T-B G-WER quality gate verifier."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import gwer_gate  # noqa: E402


def _pilot_wer(value: float = 0.18) -> dict[str, object]:
    return {
        "tag": "pilot-large-v3-turbo",
        "dataset_kind": "pilot-meeting",
        "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "compute": "float16",
        "n_samples": 8,
        "wer": value,
        "ref_words": 1200,
        "rtf": 0.08,
        "p50_ms": 420,
    }


def _pilot_der(value: float = 0.22) -> dict[str, object]:
    return {
        "tag": "pilot-pyannote",
        "fixture_kind": "pilot-meeting",
        "backend": "pyannote",
        "model": "pyannote/speaker-diarization-3.1",
        "n_samples": 8,
        "der_corpus": value,
        "collar": 0.25,
        "rtf": 0.04,
        "p50_ms": 1200,
    }


def test_gate_passes_with_pilot_wer_and_der_under_threshold() -> None:
    result = gwer_gate.evaluate_gate(
        wer_rows=[_pilot_wer(0.18)],
        der_rows=[_pilot_der(0.22)],
        max_wer=0.25,
        max_der=0.30,
    )

    assert result["status"] == "pass"
    assert result["findingCount"] == 0
    assert result["selectedWer"]["value"] == 0.18
    assert result["selectedDer"]["value"] == 0.22


def test_common_voice_and_synthetic_do_not_satisfy_pilot_gate() -> None:
    result = gwer_gate.evaluate_gate(
        wer_rows=[
            {
                "tag": "large-v3-turbo-fp16",
                "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
                "wer": 0.183,
                "n_samples": 150,
            },
            {
                "tag": "turbo-synth",
                "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
                "wer": 0.2558,
                "n_samples": 150,
            },
        ],
        der_rows=[
            {
                "tag": "pyannote-collar025",
                "fixture_kind": "synthetic-smoke",
                "backend": "pyannote",
                "der_corpus": 0.478,
                "n_samples": 6,
            }
        ],
        max_wer=0.25,
        max_der=0.30,
    )

    assert result["status"] == "blocked"
    assert "legacy-common-voice" in result["werKinds"]
    assert "synthetic-degraded" in result["werKinds"]
    assert "synthetic-smoke" in result["derKinds"]
    assert any("no pilot-meeting WER row" in finding for finding in result["findings"])
    assert any("no pilot-meeting DER row" in finding for finding in result["findings"])


def test_gate_fails_when_pilot_metric_exceeds_threshold() -> None:
    result = gwer_gate.evaluate_gate(
        wer_rows=[_pilot_wer(0.31)],
        der_rows=[_pilot_der(0.22)],
        max_wer=0.25,
        max_der=0.30,
    )

    assert result["status"] == "fail"
    assert result["findingCount"] == 1
    assert "exceeds threshold" in result["findings"][0]


def test_thresholds_are_required() -> None:
    result = gwer_gate.evaluate_gate(
        wer_rows=[_pilot_wer(0.18)],
        der_rows=[_pilot_der(0.22)],
        max_wer=None,
        max_der=0.30,
    )

    assert result["status"] == "blocked"
    assert "explicit max_wer and max_der thresholds are required" in result["findings"]


@pytest.mark.parametrize(
    "unsafe_key",
    ["audio_path", "hypothesis", "reference", "transcript", "wav"],
)
def test_gate_rejects_raw_audio_or_text_fields(unsafe_key: str) -> None:
    row = _pilot_wer(0.18)
    row[unsafe_key] = "customer-meeting.wav" if unsafe_key in {"audio_path", "wav"} else "raw text"

    result = gwer_gate.evaluate_gate(
        wer_rows=[row],
        der_rows=[_pilot_der(0.22)],
        max_wer=0.25,
        max_der=0.30,
    )

    assert result["status"] == "fail"
    assert unsafe_key in result["findings"][0]


def test_gate_rejects_nested_audio_or_text_fields() -> None:
    row = _pilot_der(0.22)
    row["debug"] = {
        "segments": [{"speaker": "SPEAKER_00", "audio_url": "sample.flac"}],
    }

    result = gwer_gate.evaluate_gate(
        wer_rows=[_pilot_wer(0.18)],
        der_rows=[row],
        max_wer=0.25,
        max_der=0.30,
    )

    assert result["status"] == "fail"
    assert any("debug.segments" in finding for finding in result["findings"])
    assert all("sample.flac" not in finding for finding in result["findings"])


def test_load_rows_supports_jsonl_and_object_with_rows(tmp_path: Path) -> None:
    jsonl = tmp_path / "wer.jsonl"
    jsonl.write_text(
        json.dumps(_pilot_wer(0.18), ensure_ascii=False) + "\n"
        + json.dumps(_pilot_wer(0.19), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    assert len(gwer_gate._load_rows(jsonl)) == 2

    wrapped = tmp_path / "der.json"
    wrapped.write_text(json.dumps({"rows": [_pilot_der(0.22)]}), encoding="utf-8")
    assert len(gwer_gate._load_rows(wrapped)) == 1
