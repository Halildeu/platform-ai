"""Pydantic request/response schemas for diarization."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SpeakerSegment(BaseModel):
    """A diarized turn — which speaker spoke when.

    `speaker` is an anonymous label (e.g. SPEAKER_00). It is NOT a voiceprint
    or any biometric identity — speaker identification is a separate, consented,
    later phase (issue #48 scope note).
    """

    speaker: str = Field(description="Anonymous speaker label, e.g. SPEAKER_00")
    start: float = Field(description="Turn start (seconds)", ge=0.0)
    end: float = Field(description="Turn end (seconds)", ge=0.0)


class DiarizeResponse(BaseModel):
    """Diarization response."""

    segments: list[SpeakerSegment] = Field(default_factory=list)
    num_speakers: int = Field(description="Distinct speakers detected", ge=0)
    duration: float = Field(description="Audio duration (seconds)", ge=0.0)
    elapsed_ms: int = Field(description="Diarization wall-clock", ge=0)
    model: str = Field(description="Pipeline used")
    device: str = Field(description="cpu / cuda")
    backend: str = Field(description="mock / pyannote")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(description="ok / degraded / loading")
    version: str
    model: str
    device: str
    backend: str


class ErrorResponse(BaseModel):
    """Standardized error envelope."""

    error: str
    detail: str | None = None
