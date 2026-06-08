"""Queue and transcription schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TranscriptSegment(BaseModel):
    id: int = Field(ge=0)
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


class FinalSttJob(BaseModel):
    session_id: str = Field(alias="sessionId", min_length=1, max_length=200)
    chunk_seq: int = Field(alias="chunkSeq", ge=0)
    audio_path: str = Field(alias="audioPath", min_length=1)
    audio_duration_sec: float = Field(alias="audioDurationSec", gt=0.0)
    committed_text: str = Field(default="", alias="committedText", max_length=100000)
    draft_text: str = Field(default="", alias="draftText", max_length=50000)
    correlation_id: str = Field(alias="correlationId", min_length=1, max_length=200)


class FinalSttResult(BaseModel):
    session_id: str = Field(alias="sessionId")
    chunk_seq: int = Field(alias="chunkSeq")
    correlation_id: str = Field(alias="correlationId")
    revised_text: str = Field(alias="revisedText")
    final_chunk_text: str = Field(alias="finalChunkText")
    draft_text: str = Field(alias="draftText")
    overlap_words: int = Field(alias="overlapWords", ge=0)
    language: str
    language_probability: float = Field(alias="languageProbability", ge=0.0, le=1.0)
    audio_duration_sec: float = Field(alias="audioDurationSec", ge=0.0)
    elapsed_ms: int = Field(alias="elapsedMs", ge=0)
    model: str
    compute_type: str = Field(alias="computeType")
    device: str
    segments: list[TranscriptSegment] = Field(default_factory=list)


class HealthResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    status: str
    version: str
    model: str
    model_revision: str
    device: str
    compute_type: str
    redis_enabled: bool
