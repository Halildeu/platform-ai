"""Pydantic request/response schemas for meeting-ai."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    """An STT segment with wall-clock timing (Whisper-style)."""

    text: str = Field(description="Segment text")
    start: float = Field(description="Segment start (seconds)", ge=0.0)
    end: float | None = Field(default=None, description="Segment end (seconds)")


class AnalyzeRequest(BaseModel):
    """Transcript analysis request."""

    transcript: str = Field(description="Meeting transcript text", min_length=1)
    meeting_id: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    segments: list[TranscriptSegment] | None = Field(
        default=None,
        description="Optional STT timing; enables wall-clock start_sec on citations",
    )


class ActionItem(BaseModel):
    """One extracted action item."""

    text: str = Field(description="Action description")
    owner: str | None = Field(default=None, description="Owner if detected")


class Citation(BaseModel):
    """#162: a decision/action grounded to its source transcript sentence."""

    claim: str = Field(description="The decision/action text")
    source_index: int = Field(description="Source sentence index, -1 if ungrounded")
    source_text: str = Field(default="", description="The transcript sentence it came from")
    similarity: float = Field(description="Token-overlap score with the source", ge=0.0, le=1.0)
    grounded: bool = Field(description="False = ungrounded (possible hallucination)")
    start_sec: float | None = Field(
        default=None, description="Source sentence start (sec) when STT timing is available"
    )


class AnalyzeResponse(BaseModel):
    """Summary + decisions + action items + #162 citations."""

    summary: str = Field(description="Short meeting summary")
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    citations: list[Citation] = Field(
        default_factory=list, description="#162: each decision/action grounded to transcript"
    )
    ungrounded_count: int = Field(
        default=0, description="#162: claims not grounded in transcript (hallucination guard)", ge=0
    )
    redacted: bool = Field(description="Whether PII redaction ran before analysis")
    redaction_count: int = Field(description="PII spans redacted", ge=0)
    backend: str = Field(description="mock / anthropic / openai / ollama")
    model: str = Field(description="Model/pipeline used")
    elapsed_ms: int = Field(description="Analysis wall-clock", ge=0)


class AskRequest(BaseModel):
    """#162 PR-llm-03: post-meeting ask-AI over a transcript."""

    transcript: str = Field(description="Meeting transcript", min_length=1)
    question: str = Field(description="Question about the meeting", min_length=1)
    meeting_id: str | None = Field(default=None, max_length=64)


class AskResponse(BaseModel):
    """Answer grounded to the transcript (citation + hallucination guard)."""

    answer: str = Field(description="Answer derived from the transcript")
    citation: Citation = Field(description="Transcript sentence the answer is grounded to")
    grounded: bool = Field(description="False = answer not supported by transcript")
    redacted: bool = Field(description="Whether PII redaction ran")
    backend: str = Field(description="mock / ollama")
    elapsed_ms: int = Field(ge=0)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(description="ok / degraded / loading")
    version: str
    backend: str
    model: str
    redact_pii: bool


class ErrorResponse(BaseModel):
    """Standardized error envelope."""

    error: str
    detail: str | None = None
