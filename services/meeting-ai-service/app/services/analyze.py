"""Meeting analysis service facade.

Backends:
- `mock` (default): deterministic, keyword-based extractive summary/decisions/
  actions — no LLM call, no API key, unit-testable.
- `ollama`: real LLM via local Ollama server (Option B, #54 decision).
  Intended on-prem (transcript stays in-cluster); the actual network boundary is
  enforced at deploy time by a GitOps NetworkPolicy, not by this code (ADR-0034).
- `anthropic` / `openai`: stubs (501); require ADR-0030 legal gate (#52).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse, Citation
from app.services.citation import ground_claims
from app.services.redact import redact_pii


@dataclass
class AnalysisDraft:
    """Backend-agnostic analysis result before HTTP serialization."""

    summary: str = ""
    decisions: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)


class BackendUnavailableError(RuntimeError):
    """Raised when a real LLM backend is unreachable or returns unusable output.

    Message must stay transcript-free (KVKK): only error class/HTTP detail.
    """


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


_OLLAMA_PROMPT = """\
Sen Türkçe toplantı tutanakları analiz eden bir asistansın. Aşağıdaki toplantı \
metnini incele ve JSON formatında yanıt ver.

Metin:
{transcript}

Lütfen sadece geçerli JSON döndür, başka bir şey ekleme:
{{
  "summary": "<maksimum 3 cümle toplantı özeti>",
  "decisions": ["<karar 1>", "<karar 2>"],
  "action_items": [
    {{"text": "<aksiyon açıklaması>", "owner": "<sorumlu kişi veya null>"}}
  ]
}}
"""


class OllamaAnalyzer:
    """Local Ollama LLM backend (Option B, #54). Intended on-prem; the on-host
    boundary is enforced by a deploy-time NetworkPolicy, not by this code (ADR-0034)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(self, transcript: str) -> AnalysisDraft:
        prompt = _OLLAMA_PROMPT.format(transcript=transcript)
        payload = {
            "model": self._settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",  # force structured JSON (llama3.1 else returns prose)
        }
        try:
            resp = httpx.post(
                f"{self._settings.ollama_host}/api/generate",
                json=payload,
                timeout=self._settings.request_timeout,
            )
            resp.raise_for_status()
            raw_text = resp.json().get("response", "")
            # Strip markdown code fences if Ollama wraps JSON
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip())
            data = json.loads(cleaned)
            draft = AnalysisDraft(
                summary=str(data.get("summary", "")),
                decisions=[str(d) for d in data.get("decisions", [])],
                action_items=[
                    ActionItem(text=str(a.get("text", "")), owner=a.get("owner"))
                    for a in data.get("action_items", [])
                ],
            )
        except httpx.HTTPError as exc:
            # Transcript-free message (KVKK): class name + endpoint only.
            raise BackendUnavailableError(
                f"Ollama unreachable or returned HTTP error ({type(exc).__name__})"
            ) from exc
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as exc:
            raise BackendUnavailableError(
                f"Ollama returned unparseable output ({type(exc).__name__})"
            ) from exc

        return draft

    @property
    def model_loaded(self) -> bool:
        try:
            r = httpx.get(f"{self._settings.ollama_host}/api/tags", timeout=3)
            return r.status_code == 200
        except httpx.HTTPError:
            return False


class LlmStubAnalyzer:
    """Anthropic/OpenAI — stubs; require legal gate (#52/ADR-0030)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(self, transcript: str) -> AnalysisDraft:
        raise NotImplementedError(
            f"backend '{self._settings.backend}' requires ADR-0030 legal approval (#52). "
            "Use MAI_BACKEND=mock or MAI_BACKEND=ollama."
        )

    @property
    def model_loaded(self) -> bool:
        return False


def build_analyzer(settings: Settings) -> Analyzer:
    if settings.backend == "mock":
        return MockAnalyzer(settings)
    if settings.backend == "ollama":
        return OllamaAnalyzer(settings)
    return LlmStubAnalyzer(settings)


class MeetingAnalysisService:
    """Redact-then-analyze meeting AI service."""

    def __init__(self, settings: Settings, analyzer: Analyzer | None = None) -> None:
        self._settings = settings
        self._analyzer = analyzer or build_analyzer(settings)

    def analyze(
        self, transcript: str, segments: list[dict[str, object]] | None = None
    ) -> AnalyzeResponse:
        start = time.perf_counter()
        if self._settings.redact_pii:
            redacted, count = redact_pii(transcript)
        else:
            redacted, count = transcript, 0

        # The analyzer only ever sees redacted text.
        draft = self._analyzer.analyze(redacted)

        # #162: ground every decision/action to a transcript sentence (citation)
        # and flag the ungrounded ones (hallucination guard). Grounded against the
        # SAME redacted text the analyzer saw, so claims and sources line up.
        # Optional STT `segments` attach a wall-clock start_sec to each citation.
        claims = list(draft.decisions) + [a.text for a in draft.action_items]
        grounded, ungrounded = ground_claims(claims, redacted, segments=segments)
        citations = [
            Citation(
                claim=c.claim,
                source_index=c.source_index,
                source_text=c.source_text,
                similarity=c.similarity,
                grounded=c.grounded,
                start_sec=c.start_sec,
            )
            for c in grounded
        ]
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return AnalyzeResponse(
            summary=draft.summary,
            decisions=draft.decisions,
            action_items=draft.action_items,
            citations=citations,
            ungrounded_count=ungrounded,
            redacted=self._settings.redact_pii,
            redaction_count=count,
            backend=self._settings.backend,
            model=self.effective_model,
            elapsed_ms=elapsed_ms,
        )

    @property
    def effective_model(self) -> str:
        """The model actually used (delegates to Settings for one source of truth)."""
        return self._settings.effective_model

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
