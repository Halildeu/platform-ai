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
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse, Citation, RejectedClaim
from app.services.citation import Citation as GroundedCitation
from app.services.citation import ground_claim, split_sentences
from app.services.redact import assert_no_residual_pii, redact_pii


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


class OllamaSchemaInvalidError(BackendUnavailableError):
    """The LLM returned parseable JSON that violates the analysis schema.

    A subclass of BackendUnavailableError so the API still fails closed (502), but
    distinct so the bakeoff separates 'the model broke the contract' (a model
    quality signal → schema_invalid) from 'the backend/host failed' (infra →
    backend_error). Conflating them would report a host outage as 100% schema
    failure (Codex review).
    """


class OllamaUnparseableOutputError(BackendUnavailableError):
    """The LLM returned output that is not even valid JSON.

    Still a MODEL output-contract failure (under format=json the model produced
    garbage), NOT infra — so the bakeoff counts it as format_invalid, distinct
    from backend_error (host/HTTP/timeout). Classifying unparseable model output
    as 'infra' would understate a model's true contract-failure rate (Codex review).
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


def _require_ollama_schema(data: object) -> dict[str, Any]:
    """Element-level strict shape check on the LLM's JSON (Codex review #162).

    A schema break (missing key, wrong type — ``decisions`` as a string that would
    become a per-character list, a decision that is an object, an action ``text``
    that is a list, an ``owner`` that is an int) MUST be distinguishable from a
    legitimate "no decisions": otherwise a model that breaks the contract is
    mis-scored as merely low-recall. We fail closed with ``OllamaSchemaInvalidError``
    and the eval counts these as ``schema_invalid`` (a model-quality signal),
    separate from infra/backend failures.
    """
    if not isinstance(data, dict):
        raise OllamaSchemaInvalidError("Ollama JSON is not an object")
    summary = data.get("summary")
    decisions = data.get("decisions")
    actions = data.get("action_items")
    if not isinstance(summary, str):
        raise OllamaSchemaInvalidError("field 'summary' must be a string")
    if not isinstance(decisions, list) or not all(isinstance(d, str) for d in decisions):
        raise OllamaSchemaInvalidError("field 'decisions' must be a list of strings")
    if not isinstance(actions, list):
        raise OllamaSchemaInvalidError("field 'action_items' must be a list")
    for item in actions:
        if not isinstance(item, dict):
            raise OllamaSchemaInvalidError("action_items entry must be an object")
        if not isinstance(item.get("text"), str):
            raise OllamaSchemaInvalidError("action_items[].text must be a string")
        owner = item.get("owner")
        if owner is not None and not isinstance(owner, str):
            raise OllamaSchemaInvalidError("action_items[].owner must be a string or null")
    return data


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

ÖNEMLİ KURALLAR:
- Yanıt dili Türkçe olsun; ama metinde geçen özel adları, ürün/proje adlarını, \
teknik terimleri ve İngilizce code-switch ifadelerini (ör. "deadline", "sprint") \
aynen koru.
- Karar ve aksiyonları metinde GEÇEN ifadelere sadık kal; metinde olmayan bilgi \
ekleme, uydurma.
- Metinde karar yoksa "decisions" boş liste; aksiyon yoksa "action_items" boş \
liste döndür.

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
            # Deterministic extraction + no transcript truncation (see config: the
            # 2048-default num_ctx silently cut long meetings; 0.8-default temperature
            # made the eval non-reproducible). One source of truth in Settings.
            "options": self._settings.ollama_options(),
            "keep_alive": self._settings.ollama_keep_alive,
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
            data = _require_ollama_schema(json.loads(cleaned))
            draft = AnalysisDraft(
                summary=str(data["summary"]),
                decisions=[str(d) for d in data["decisions"]],
                action_items=[
                    ActionItem(text=str(a["text"]), owner=a.get("owner"))
                    for a in data["action_items"]
                ],
            )
        except httpx.HTTPError as exc:
            # Transcript-free message (KVKK): class name + endpoint only.
            raise BackendUnavailableError(
                f"Ollama unreachable or returned HTTP error ({type(exc).__name__})"
            ) from exc
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as exc:
            raise OllamaUnparseableOutputError(
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


def _to_schema_citation(c: GroundedCitation) -> Citation:
    """Map a verified service citation → API schema citation (ADR-0043 D4 fields)."""
    return Citation(
        claim=c.claim,
        source_index=c.source_index,
        source_text=c.source_text,
        similarity=c.similarity,
        grounded=c.grounded,
        status=c.status.value,
        reason=c.reason,
        start_sec=c.start_sec,
        source_char_start=c.source_char_start,
        source_char_end=c.source_char_end,
        source_hash=c.source_hash,
        quote_hash=c.quote_hash,
    )


def _to_rejected(claim: str, kind: str, c: GroundedCitation) -> RejectedClaim:
    """ADR-0043 D8.1: an ungrounded/contradicted claim withheld from the output."""
    return RejectedClaim(
        claim=claim,
        kind=kind,
        status=c.status.value,
        reason=c.reason,
        similarity=c.similarity,
    )


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

        # ADR-0043 D3 fail-closed (Codex 019ee9a6): run the residual gate whenever
        # redaction ran — BACKEND-INDEPENDENT, so an accidentally-enabled mock in a
        # deployed env can't bypass it (the config validator also hard-fails mock in
        # stage/prod). `redact_pii=False` is the only opt-out (local mock fixtures).
        # Raises RedactionError → 422 at the API layer.
        if self._settings.redact_pii:
            assert_no_residual_pii(redacted)

        # The analyzer only ever sees redacted text.
        draft = self._analyzer.analyze(redacted)

        # ADR-0043 D4 + D8.1: ground + entailment-check every decision/action against
        # the SAME redacted text the analyzer saw. Ship ONLY the grounded (PASSED)
        # claims; withhold ungrounded/contradicted ones into `rejected_claims` so they
        # are auditable but never presented as fact (fail-closed hallucination guard).
        sentences = split_sentences(redacted, segments)
        kept_decisions: list[str] = []
        kept_actions: list[ActionItem] = []
        citations: list[Citation] = []
        rejected: list[RejectedClaim] = []

        for decision in draft.decisions:
            if not decision.strip():
                continue
            verdict = ground_claim(decision, sentences)
            if verdict.grounded:
                kept_decisions.append(decision)
                citations.append(_to_schema_citation(verdict))
            else:
                rejected.append(_to_rejected(decision, "decision", verdict))

        for action in draft.action_items:
            if not action.text.strip():
                continue
            verdict = ground_claim(action.text, sentences)
            if verdict.grounded:
                kept_actions.append(action)
                citations.append(_to_schema_citation(verdict))
            else:
                rejected.append(_to_rejected(action.text, "action", verdict))

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return AnalyzeResponse(
            summary=draft.summary,
            decisions=kept_decisions,
            action_items=kept_actions,
            citations=citations,
            rejected_claims=rejected,
            ungrounded_count=len(rejected),
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
