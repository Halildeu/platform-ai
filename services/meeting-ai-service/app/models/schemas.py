"""Pydantic request/response schemas for meeting-ai."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    """Transcript analysis request."""

    transcript: str = Field(description="Meeting transcript text", min_length=1)
    meeting_id: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)


class ActionItem(BaseModel):
    """One extracted action item."""

    text: str = Field(description="Action description")
    owner: str | None = Field(default=None, description="Owner if detected")


class AnalyzeResponse(BaseModel):
    """Summary + decisions + action items."""

    summary: str = Field(description="Short meeting summary")
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    redacted: bool = Field(description="Whether PII redaction ran before analysis")
    redaction_count: int = Field(description="PII spans redacted", ge=0)
    backend: str = Field(description="mock / anthropic / openai / ollama")
    model: str = Field(description="Model/pipeline used")
    elapsed_ms: int = Field(description="Analysis wall-clock", ge=0)


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
