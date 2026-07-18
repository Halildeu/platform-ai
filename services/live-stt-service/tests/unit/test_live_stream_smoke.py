"""Tests for the transcript-free live stream smoke client."""

# ruff: noqa: RUF001 - Turkish fixture text is intentional.

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "live_stream_smoke.py"
FIXTURE = ROOT / "tests" / "fixtures" / "sample-tr-cv17-001.wav"


def _load_smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_stream_smoke", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wav_loader_resamples_common_voice_fixture_to_16khz_float32() -> None:
    smoke = _load_smoke_module()

    audio = smoke.load_wav_float32(FIXTURE)

    assert audio.dtype.name == "float32"
    assert 88_000 <= audio.shape[0] <= 89_000
    assert float(audio.max()) <= 1.0
    assert float(audio.min()) >= -1.0


def test_redacted_summary_excludes_transcript_text() -> None:
    smoke = _load_smoke_module()
    raw_text = "Kelime akışı aktif ve doğruluk oranı gayet iyi."
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            smoke.redacted_transcript_event(
                {
                    "type": "partial",
                    "seq": 0,
                    "confirmed": "",
                    "tentative": "Kelime akışı",
                    "elapsed_ms": 400,
                    "rms": 0.03,
                },
                500,
            ),
            smoke.redacted_transcript_event(
                {
                    "type": "final",
                    "seq": 0,
                    "text": raw_text,
                    "elapsed_ms": 700,
                    "rms": 0.03,
                },
                1500,
            ),
        ],
        terminal_events=["eof_ack", "drained"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
    )
    payload = json.dumps(summary, ensure_ascii=False)
    raw_reference = FIXTURE.with_suffix(".txt").read_text(encoding="utf-8").strip()

    assert summary["ok"] is True
    assert summary["privacy"] == {
        "raw_audio_logged": False,
        "transcript_text_logged": False,
        "hashes_only": True,
    }
    assert raw_text not in payload
    assert raw_reference not in payload
    assert "text_sha256_12" in payload
    assert "text_words" in payload
    assert summary["coverage"]["reference_words"] == 5
    assert summary["quality_gate"]["failures"] == []


def test_summary_fails_when_final_word_coverage_is_too_low() -> None:
    smoke = _load_smoke_module()
    raw_text = "Eksik"
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            smoke.redacted_transcript_event(
                {
                    "type": "partial",
                    "seq": 0,
                    "confirmed": "",
                    "tentative": "Eksik",
                    "elapsed_ms": 300,
                    "rms": 0.03,
                },
                450,
            ),
            smoke.redacted_transcript_event(
                {
                    "type": "final",
                    "seq": 0,
                    "text": raw_text,
                    "elapsed_ms": 700,
                    "rms": 0.03,
                },
                1500,
            ),
        ],
        terminal_events=["eof_ack", "drained"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
        min_final_word_coverage=0.5,
    )
    payload = json.dumps(summary, ensure_ascii=False)

    assert summary["ok"] is False
    assert summary["coverage"] == {
        "final_words": 1,
        "reference_words": 5,
        "final_word_coverage": 0.2,
    }
    assert "final_word_coverage_below_min" in summary["quality_gate"]["failures"]
    assert raw_text not in payload


def test_summary_rejects_missing_terminal_drain() -> None:
    smoke = _load_smoke_module()
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            {"type": "partial", "received_at_ms": 100},
            {"type": "final", "received_at_ms": 200, "text_words": 5},
        ],
        terminal_events=["eof_ack"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
    )

    assert summary["ok"] is False
    assert "terminal_sequence_invalid" in summary["quality_gate"]["failures"]


def test_final_event_count_honors_requested_long_smoke_gate() -> None:
    smoke = _load_smoke_module()

    events = [
        {"type": "partial"},
        {"type": "final"},
        {"type": "partial"},
        {"type": "final"},
    ]

    assert smoke.final_event_count(events) == 2
    assert smoke.final_event_count(events) < 3
