"""#3746 AI-D3 — attribution job orchestration tests.

Direction: every early exit must be fail-closed (no publish), the VRAM
deferral loop must be bounded, and the published payload must pass the
committed contract schema (validated inside the job itself).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.session_attribution import DiarTurn
from app.services.session_attribution_job import (
    FinishEnvelope,
    JobStatus,
    run_attribution_job,
)
from app.services.session_audio_store import SessionAudio
from app.services.session_diarization_runner import (
    DiarizationOutcome,
    DiarizationStatus,
    RunnerConfig,
)

MEETING = "9b2c5a89-f39a-47da-a33d-a2859b468e8b"
SECOND_BYTES = 16_000 * 2


def make_envelope(**overrides: object) -> FinishEnvelope:
    base: dict[str, object] = {
        "tenant_id": "42",
        "meeting_id": MEETING,
        "source_session_id": "SES-1",
        "transport_epoch": 0,
        "expected_sample_count": 16_000,
    }
    base.update(overrides)
    return FinishEnvelope(**base)  # type: ignore[arg-type]


def make_runner_config() -> RunnerConfig:
    return RunnerConfig(
        model_name="pyannote/speaker-diarization-3.1",
        model_revision="84fd25912480287da0247647c3d2b4853cb3ee5d",
        hf_token="unused",
        max_speakers=10,
    )


def make_audio() -> SessionAudio:
    return SessionAudio(pcm16=b"\x00" * SECOND_BYTES, windows=((0, 0, 1000),))


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


def done_runner(turns: list[DiarTurn]):
    def _runner(wav_bytes: bytes, config: RunnerConfig) -> DiarizationOutcome:
        return DiarizationOutcome(
            status=DiarizationStatus.DONE,
            turns=turns,
            model_revision=config.model_revision,
            elapsed_ms=10,
        )

    return _runner


def test_happy_path_publishes_schema_valid_event() -> None:
    publisher = FakePublisher()
    result = run_attribution_job(
        make_audio(),
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=done_runner([DiarTurn("SPEAKER_00", 0, 950)]),
        now=lambda: datetime(2026, 9, 29, 13, 0, 0, tzinfo=UTC),
    )
    assert result.status is JobStatus.PUBLISHED
    assert result.window_count == 1
    assert result.uu_window_count == 0
    [event] = publisher.published
    assert event["schema"] == "directSttSessionAttribution.v1"
    assert event["windows"] == [
        {
            "windowSeq": 0,
            "speaker": "SPEAKER_00",
            "startMs": 0,
            "endMs": 1000,
            "dominanceRatio": 1.0,
        }
    ]
    assert event["audioSampleCount"] == 16_000
    assert event["generatedAt"] == "2026-09-29T13:00:00Z"


def test_no_audio_cancels_without_publish() -> None:
    publisher = FakePublisher()
    result = run_attribution_job(
        None,
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=done_runner([]),
    )
    assert result.status is JobStatus.CANCELLED_NO_AUDIO
    assert publisher.published == []


def test_sample_mismatch_cancels_without_publish() -> None:
    publisher = FakePublisher()
    result = run_attribution_job(
        make_audio(),
        make_envelope(expected_sample_count=17_000),
        make_runner_config(),
        publisher=publisher,
        runner=done_runner([DiarTurn("S1", 0, 900)]),
    )
    assert result.status is JobStatus.CANCELLED_SAMPLE_MISMATCH
    assert "expected=17000" in result.detail
    assert publisher.published == []


def test_missing_window_map_cancels() -> None:
    publisher = FakePublisher()
    audio = SessionAudio(pcm16=b"\x00" * SECOND_BYTES, windows=())
    result = run_attribution_job(
        audio,
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=done_runner([DiarTurn("S1", 0, 900)]),
    )
    assert result.status is JobStatus.CANCELLED_NO_WINDOWS
    assert publisher.published == []


def test_vram_deferral_retries_are_bounded() -> None:
    publisher = FakePublisher()
    calls: list[float] = []
    attempts = {"n": 0}

    def deferring_runner(wav_bytes: bytes, config: RunnerConfig) -> DiarizationOutcome:
        attempts["n"] += 1
        return DiarizationOutcome(status=DiarizationStatus.DEFERRED_VRAM, detail="free_mb=500")

    result = run_attribution_job(
        make_audio(),
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=deferring_runner,
        vram_retry_attempts=2,
        vram_retry_backoff_sec=7.0,
        sleep=calls.append,
    )
    assert result.status is JobStatus.DEFERRED_GAVE_UP
    assert attempts["n"] == 3  # initial + 2 retries
    assert calls == [7.0, 7.0]
    assert publisher.published == []


def test_deferral_then_success_publishes() -> None:
    publisher = FakePublisher()
    replies = iter(
        [
            DiarizationOutcome(status=DiarizationStatus.DEFERRED_VRAM, detail="free_mb=1"),
            DiarizationOutcome(
                status=DiarizationStatus.DONE,
                turns=[DiarTurn("S1", 0, 900)],
                model_revision="rev-a",
            ),
        ]
    )
    result = run_attribution_job(
        make_audio(),
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=lambda wav, cfg: next(replies),
        sleep=lambda s: None,
    )
    assert result.status is JobStatus.PUBLISHED
    assert publisher.published[0]["modelRevision"] == "rev-a"


def test_runner_failure_never_publishes() -> None:
    publisher = FakePublisher()
    result = run_attribution_job(
        make_audio(),
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=lambda wav, cfg: DiarizationOutcome(
            status=DiarizationStatus.TIMEOUT_KILLED, detail="hard_timeout"
        ),
    )
    assert result.status is JobStatus.FAILED
    assert result.detail == "hard_timeout"
    assert publisher.published == []


def test_contested_windows_count_as_uu() -> None:
    publisher = FakePublisher()
    result = run_attribution_job(
        make_audio(),
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=done_runner([DiarTurn("SPEAKER_00", 0, 500), DiarTurn("SPEAKER_01", 500, 1000)]),
    )
    assert result.status is JobStatus.PUBLISHED
    assert result.uu_window_count == 1
    assert publisher.published[0]["windows"][0]["speaker"] == "UU"


def test_schema_regression_fails_before_publish() -> None:
    # An invalid model revision (empty) must be caught by the in-job schema
    # validation — the publisher must never see a non-conforming payload.
    publisher = FakePublisher()
    result = run_attribution_job(
        make_audio(),
        make_envelope(),
        make_runner_config(),
        publisher=publisher,
        runner=lambda wav, cfg: DiarizationOutcome(
            status=DiarizationStatus.DONE,
            turns=[DiarTurn("S1", 0, 900)],
            model_revision="",
        ),
    )
    assert result.status is JobStatus.FAILED
    assert result.detail.startswith("payload_schema:")
    assert publisher.published == []


def test_envelope_bounds() -> None:
    with pytest.raises(ValueError):
        make_envelope(tenant_id="")
    with pytest.raises(ValueError):
        make_envelope(expected_sample_count=100)
