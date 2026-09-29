"""Post-session attribution job orchestration (#3746 AI-D3, part 5).

One job per finished session: store hand-off -> whole-session integrity
check -> in-RAM WAV framing -> supervised diarization batch (bounded
VRAM-deferral retries) -> per-window dominance reduction -> schema-validated
``directSttSessionAttribution.v1`` publish. Every early exit is fail-closed
to "no attribution" (the event is simply never published) — partial or
mismatched audio must never mint labels.

The job validates its own payload against the committed contract schema
before publishing, so a producer regression fails here (metadata-only error)
instead of poisoning the consumer.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import jsonschema  # type: ignore[import-untyped]

from app.services.session_attribution import (
    WindowSpan,
    assign_windows,
    pcm16_to_wav_bytes,
)
from app.services.session_audio_store import SessionAudio
from app.services.session_diarization_runner import (
    DiarizationOutcome,
    DiarizationStatus,
    RunnerConfig,
    run_session_diarization,
)

__all__ = [
    "AttributionPublisher",
    "FinishEnvelope",
    "JobResult",
    "JobStatus",
    "run_attribution_job",
]

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "contracts"
    / "direct-stt-session-attribution.v1.schema.json"
)
_schema_cache: dict[str, Any] | None = None


def _event_schema() -> dict[str, Any]:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


class JobStatus(Enum):
    PUBLISHED = "published"
    CANCELLED_NO_AUDIO = "cancelled_no_audio"
    CANCELLED_SAMPLE_MISMATCH = "cancelled_sample_mismatch"
    CANCELLED_NO_WINDOWS = "cancelled_no_windows"
    DEFERRED_GAVE_UP = "deferred_gave_up"
    FAILED = "failed"


@dataclass(frozen=True)
class JobResult:
    status: JobStatus
    # Non-content diagnostics only (counts, status names) — never text/audio.
    detail: str = ""
    window_count: int = 0
    uu_window_count: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True)
class FinishEnvelope:
    """Identity + integrity facts from liveSttSessionFinish.v1 (design D4)."""

    tenant_id: str
    meeting_id: str
    source_session_id: str
    transport_epoch: int
    expected_sample_count: int

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.meeting_id or not self.source_session_id:
            raise ValueError("identity envelope fields are required")
        if self.transport_epoch < 0 or self.expected_sample_count < 16_000:
            raise ValueError("epoch/sample bounds violated")


class AttributionPublisher(Protocol):
    """Delivery seam; the Redis implementation lives beside the chunk client."""

    def publish(self, payload: dict[str, Any]) -> None: ...  # pragma: no cover - protocol


def run_attribution_job(
    audio: SessionAudio | None,
    envelope: FinishEnvelope,
    runner_config: RunnerConfig,
    *,
    publisher: AttributionPublisher,
    runner: Callable[..., DiarizationOutcome] = run_session_diarization,
    dominance_threshold: float = 0.7,
    min_speech_ms: int = 250,
    vram_retry_attempts: int = 3,
    vram_retry_backoff_sec: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JobResult:
    """Run one attribution batch end-to-end; never raises for runtime paths."""
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if audio is None:
        return JobResult(JobStatus.CANCELLED_NO_AUDIO, elapsed_ms=elapsed())
    actual_samples = len(audio.pcm16) // 2
    if actual_samples != envelope.expected_sample_count:
        # The gateway forwarded a different total than the store holds: the
        # session audio is not whole. Labels from it would be misaligned.
        return JobResult(
            JobStatus.CANCELLED_SAMPLE_MISMATCH,
            detail=f"expected={envelope.expected_sample_count} actual={actual_samples}",
            elapsed_ms=elapsed(),
        )
    if not audio.windows:
        return JobResult(JobStatus.CANCELLED_NO_WINDOWS, elapsed_ms=elapsed())

    wav_bytes = pcm16_to_wav_bytes(audio.pcm16)
    outcome: DiarizationOutcome | None = None
    for attempt in range(vram_retry_attempts + 1):
        outcome = runner(wav_bytes, runner_config)
        if outcome.status is not DiarizationStatus.DEFERRED_VRAM:
            break
        if attempt < vram_retry_attempts:
            sleep(vram_retry_backoff_sec)
    assert outcome is not None  # loop runs at least once
    if outcome.status is DiarizationStatus.DEFERRED_VRAM:
        return JobResult(JobStatus.DEFERRED_GAVE_UP, detail=outcome.detail, elapsed_ms=elapsed())
    if outcome.status is not DiarizationStatus.DONE:
        return JobResult(JobStatus.FAILED, detail=outcome.detail, elapsed_ms=elapsed())

    spans = [WindowSpan(seq, start, end) for seq, start, end in audio.windows]
    assignments = assign_windows(
        spans,
        outcome.turns,
        dominance_threshold=dominance_threshold,
        min_speech_ms=min_speech_ms,
    )
    payload = {
        "schema": "directSttSessionAttribution.v1",
        "tenantId": envelope.tenant_id,
        "meetingId": envelope.meeting_id,
        "sourceSessionId": envelope.source_session_id,
        "transportEpoch": envelope.transport_epoch,
        "model": runner_config.model_name,
        "modelRevision": outcome.model_revision,
        "audioSampleCount": actual_samples,
        "generatedAt": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windows": [
            {
                "windowSeq": a.window_seq,
                "speaker": a.speaker,
                "startMs": a.start_ms,
                "endMs": a.end_ms,
                "dominanceRatio": round(a.dominance_ratio, 4),
            }
            for a in assignments
        ],
    }
    try:
        jsonschema.validate(payload, _event_schema())
    except jsonschema.ValidationError as exc:
        return JobResult(
            JobStatus.FAILED,
            detail=f"payload_schema:{exc.validator}",
            elapsed_ms=elapsed(),
        )
    publisher.publish(payload)
    uu_count = sum(1 for a in assignments if a.speaker == "UU")
    return JobResult(
        JobStatus.PUBLISHED,
        window_count=len(assignments),
        uu_window_count=uu_count,
        elapsed_ms=elapsed(),
    )
