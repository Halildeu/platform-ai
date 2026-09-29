"""Session attribution core (#3746 AI-D3): window dominance + WAV framing.

Pure functions only — no model, no I/O, no store access — so the alignment
semantics pinned by the design doc (docs/faz-24-scheduled-diarization-design-3746.md,
D3/D5) are unit-testable without a GPU:

- The whole session is clustered in ONE pyannote batch; this module reduces
  the resulting time-ranged anonymous turns to **one dominant-or-UU
  assignment per source window**. It never sees window text (live-stt does
  not have it) and never emits UTF-16 offsets — the transcript-service
  consumer derives those from the window text it stores.
- ``UU`` is the honest degraded label: no cluster reaches the dominance
  threshold, or the window holds no attributed speech at all. Wrong labels
  are worse than no labels (fail-closed, same principle as the audio store).
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

__all__ = [
    "DiarTurn",
    "WindowAssignment",
    "WindowSpan",
    "assign_windows",
    "pcm16_to_wav_bytes",
]

_SAMPLE_RATE = 16_000
_UNKNOWN = "UU"


@dataclass(frozen=True)
class WindowSpan:
    """One source window's position on the session audio timeline (ms)."""

    window_seq: int
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.window_seq < 0 or self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("window span must be non-negative and non-empty")


@dataclass(frozen=True)
class DiarTurn:
    """One anonymous diarization turn from the whole-session batch (ms)."""

    speaker: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not self.speaker or self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("diarization turn must be non-negative and non-empty")


@dataclass(frozen=True)
class WindowAssignment:
    """The per-window decision published in directSttSessionAttribution.v1."""

    window_seq: int
    speaker: str
    start_ms: int
    end_ms: int
    dominance_ratio: float


def _overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def assign_windows(
    windows: list[WindowSpan],
    turns: list[DiarTurn],
    *,
    dominance_threshold: float = 0.7,
    min_speech_ms: int = 250,
) -> list[WindowAssignment]:
    """Reduce session turns to one dominant-or-UU assignment per window.

    Dominance is the leading cluster's share of the window's *attributed
    speech time* (not of the wall clock, so silence does not dilute a clear
    single speaker). Windows whose attributed speech is shorter than
    ``min_speech_ms`` are ``UU``: a fraction of a second of overlap is not
    evidence. Overlapping acoustic turns simply contribute to both clusters,
    which pushes genuinely contested windows under the threshold — exactly
    the intended ``UU`` outcome.
    """
    if not 0.0 < dominance_threshold <= 1.0:
        raise ValueError("dominance_threshold must be in (0, 1]")
    if min_speech_ms < 0:
        raise ValueError("min_speech_ms must be >= 0")
    seen: set[int] = set()
    for window in windows:
        if window.window_seq in seen:
            raise ValueError("window_seq values must be unique")
        seen.add(window.window_seq)

    assignments: list[WindowAssignment] = []
    for window in sorted(windows, key=lambda w: w.window_seq):
        speech_by_speaker: dict[str, int] = {}
        for turn in turns:
            overlap = _overlap_ms(window.start_ms, window.end_ms, turn.start_ms, turn.end_ms)
            if overlap > 0:
                speech_by_speaker[turn.speaker] = speech_by_speaker.get(turn.speaker, 0) + overlap
        total_speech = sum(speech_by_speaker.values())
        if total_speech < max(min_speech_ms, 1):
            assignments.append(
                WindowAssignment(window.window_seq, _UNKNOWN, window.start_ms, window.end_ms, 0.0)
            )
            continue
        top_speaker, top_ms = max(speech_by_speaker.items(), key=lambda item: (item[1], item[0]))
        ratio = top_ms / total_speech
        speaker = top_speaker if ratio >= dominance_threshold else _UNKNOWN
        assignments.append(
            WindowAssignment(window.window_seq, speaker, window.start_ms, window.end_ms, ratio)
        )
    return assignments


def pcm16_to_wav_bytes(pcm16: bytes, *, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Wrap raw PCM16 mono bytes in a WAV container, entirely in RAM.

    The PR #348 adapter consumes seekable in-memory WAV; nothing here may
    touch the filesystem (ADR-0036).
    """
    if not pcm16:
        raise ValueError("pcm16 payload must not be empty")
    if len(pcm16) % 2 != 0:
        raise ValueError("pcm16 payload must be an even number of bytes")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm16)
    return buffer.getvalue()
