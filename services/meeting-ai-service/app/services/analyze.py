"""Meeting analysis service facade.

Issue #49 skeleton. The service ALWAYS redacts PII before handing the transcript
to an analyzer (mock or a future real LLM), enforcing the KVKK boundary in code.

Backends:
- `mock` (default): deterministic, keyword-based extractive summary/decisions/
  actions — no LLM call, no API key, unit-testable.
- `anthropic` / `openai` / `ollama`: real LLM backends — stubs that return 501;
  wiring needs the ADR-0030 Option A/B decision + secret handling (follow-up).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse
from app.services.redact import redact_pii


@dataclass
class AnalysisDraft:
    """Backend-agnostic analysis result before HTTP serialization."""

    summary: str = ""
    decisions: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Turkish + English cue words (lowercased match).
_DECISION_CUES = ("karar", "kararlaştır", "mutabık", "onaylandı", "decided", "decision")
_ACTION_CUES = (
    "yapılacak",
    "aksiyon",
    "görev",
    "üstlen",
    "hazırla",
    "gönder",
    "takip",
    "action item",
    "todo",
    "to-do",
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _matches(sentence: str, cues: tuple[str, ...]) -> bool:
    low = sentence.lower()
    return any(cue in low for cue in cues)


class Analyzer(Protocol):
    """Minimal interface used by MeetingAnalysisService."""

    def analyze(self, transcript: str) -> AnalysisDraft:
        """Produce summary/decisions/action_items from (redacted) transcript."""

    @property
    def model_loaded(self) -> bool:
        """Whether the backend is ready to serve."""


class MockAnalyzer:
    """Deterministic keyword-based extractive analyzer (placeholder)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(self, transcript: str) -> AnalysisDraft:
        sentences = _sentences(transcript)
        summary = " ".join(sentences[:2])[: self._settings.summary_max_chars]
        decisions = [s for s in sentences if _matches(s, _DECISION_CUES)]
        action_items = [ActionItem(text=s) for s in sentences if _matches(s, _ACTION_CUES)]
        return AnalysisDraft(summary=summary, decisions=decisions, action_items=action_items)

    @property
    def model_loaded(self) -> bool:
        return True


class LlmStubAnalyzer:
    """Real LLM backends (Anthropic/OpenAI/Ollama) — not wired in the skeleton."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(self, transcript: str) -> AnalysisDraft:
        raise NotImplementedError(
            f"backend '{self._settings.backend}' is not wired yet; run with "
            "MAI_BACKEND=mock. Real LLM needs the ADR-0030 Option A/B decision "
            "+ API key / Ollama host (follow-up)."
        )

    @property
    def model_loaded(self) -> bool:
        return False


def build_analyzer(settings: Settings) -> Analyzer:
    if settings.backend == "mock":
        return MockAnalyzer(settings)
    return LlmStubAnalyzer(settings)


class MeetingAnalysisService:
    """Redact-then-analyze meeting AI service."""

    def __init__(self, settings: Settings, analyzer: Analyzer | None = None) -> None:
        self._settings = settings
        self._analyzer = analyzer or build_analyzer(settings)

    def analyze(self, transcript: str) -> AnalyzeResponse:
        start = time.perf_counter()
        if self._settings.redact_pii:
            redacted, count = redact_pii(transcript)
        else:
            redacted, count = transcript, 0

        # The analyzer only ever sees redacted text.
        draft = self._analyzer.analyze(redacted)
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return AnalyzeResponse(
            summary=draft.summary,
            decisions=draft.decisions,
            action_items=draft.action_items,
            redacted=self._settings.redact_pii,
            redaction_count=count,
            backend=self._settings.backend,
            model=self._settings.model_name,
            elapsed_ms=elapsed_ms,
        )

    @property
    def model_loaded(self) -> bool:
        return self._analyzer.model_loaded


_service: MeetingAnalysisService | None = None


def get_service(settings: Settings) -> MeetingAnalysisService:
    """Singleton accessor."""
    global _service
    if _service is None:
        _service = MeetingAnalysisService(settings)
    return _service
