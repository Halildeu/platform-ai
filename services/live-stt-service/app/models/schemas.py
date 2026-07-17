"""Pydantic request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TranscriptSegment(BaseModel):
    """Whisper segment — time-aligned transcript piece."""

    id: int = Field(description="Segment index within request")
    start: float = Field(description="Start time (seconds)", ge=0.0)
    end: float = Field(description="End time (seconds)", ge=0.0)
    text: str = Field(description="Transcribed text")
    avg_logprob: float | None = Field(default=None, description="Mean token log-prob")
    no_speech_prob: float | None = Field(default=None, description="Probability segment is silence")


class TranscribeResponse(BaseModel):
    """Synchronous transcribe response."""

    model_config = ConfigDict(protected_namespaces=())

    text: str = Field(description="Concatenated transcript")
    language: str = Field(description="Detected/forced language code (ISO 639-1)")
    language_probability: float = Field(
        description="Detection confidence (1.0 if forced)", ge=0.0, le=1.0
    )
    duration: float = Field(description="Audio duration (seconds)", ge=0.0)
    elapsed_ms: int = Field(description="Whisper inference wall-clock", ge=0)
    model: str = Field(description="Whisper model used")
    model_revision: str = Field(description="Immutable upstream model revision")
    model_sha256: str = Field(description="Verified SHA-256 of the loaded model.bin")
    compute_type: str = Field(description="Quantization (int8/float16/...)")
    device: str = Field(description="cpu / cuda")
    segments: list[TranscriptSegment] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Health check response."""

    model_config = ConfigDict(protected_namespaces=())

    status: str = Field(description="ok / degraded / loading")
    version: str
    model: str
    model_revision: str
    model_sha256: str
    device: str
    compute_type: str


class ErrorResponse(BaseModel):
    """Standardized error envelope."""

    error: str
    detail: str | None = None
