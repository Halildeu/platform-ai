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
    """#162/ADR-0043 D4: a decision/action grounded + entailment-checked to its source."""

    claim: str = Field(description="The decision/action text")
    source_index: int = Field(description="Source sentence index, -1 if ungrounded")
    source_text: str = Field(default="", description="The transcript sentence it came from")
    similarity: float = Field(description="Content-word coverage of the source", ge=0.0, le=1.0)
    grounded: bool = Field(description="True iff status==PASSED (shippable; ADR-0043 D8.1)")
    status: str = Field(default="FAILED", description="PASSED / FAILED / LOW_CONFIDENCE")
    reason: str = Field(default="", description="Why this verdict (audit)")
    start_sec: float | None = Field(
        default=None, description="Source sentence start (sec) when STT timing is available"
    )
    # ADR-0043 D4 hash/offset key — pin the citation to the exact transcript span.
    source_char_start: int = Field(default=-1, description="Source span start char (-1 ungrounded)")
    source_char_end: int = Field(default=-1, description="Source span end char (-1 ungrounded)")
    source_hash: str = Field(default="", description="sha256 of source sentence (tamper pin)")
    quote_hash: str = Field(default="", description="sha256 of quoted span (tamper pin)")


class RejectedClaim(BaseModel):
    """ADR-0043 D8.1: a claim the analyzer produced but the guard REJECTED (not shipped
    as a user-visible decision/action). Kept for transparency/audit, not as fact."""

    claim: str = Field(description="The rejected claim text")
    kind: str = Field(description="decision / action")
    status: str = Field(description="FAILED / LOW_CONFIDENCE")
    reason: str = Field(description="Why rejected (e.g. ungrounded / polarity contradiction)")
    similarity: float = Field(description="Best content-word coverage found", ge=0.0, le=1.0)


class AnalyzeResponse(BaseModel):
    """Summary + **grounded-only** decisions/action items + #162 citations (ADR-0043 D8.1).

    Contract (Codex 019ee9a6): `grounding_policy=verified_only` means decisions/
    action_items are filtered to PASSED-citation claims — an empty list means "none
    survived the guard", NOT "none produced" (see `rejected_claims`, `ungrounded_count`).
    The `summary` is NOT span-verified in v1 (`summary_grounding_status=unverified`) — it
    is narrative; only decisions/action_items carry the verified-grounding guarantee.
    """

    schema_version: str = Field(default="2-adr0043", description="Response contract version")
    grounding_policy: str = Field(
        default="verified_only", description="verified_only = decisions/actions are PASSED-only"
    )
    summary: str = Field(description="Short meeting summary (narrative, see grounding status)")
    summary_grounding_status: str = Field(
        default="unverified",
        description="v1: summary is unverified narrative; decisions/actions are verified",
    )
    decisions: list[str] = Field(
        default_factory=list, description="GROUNDED-only (ADR-0043 D8.1 fail-closed)"
    )
    action_items: list[ActionItem] = Field(
        default_factory=list, description="GROUNDED-only (ADR-0043 D8.1 fail-closed)"
    )
    citations: list[Citation] = Field(
        default_factory=list, description="PASSED citation per shipped decision/action"
    )
    rejected_claims: list[RejectedClaim] = Field(
        default_factory=list,
        description="ADR-0043 D8.1: ungrounded/contradicted claims withheld from the output",
    )
    ungrounded_count: int = Field(
        default=0, description="#162: claims rejected by the hallucination guard", ge=0
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
