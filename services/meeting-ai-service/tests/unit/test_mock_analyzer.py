"""Mock analyzer + redact-then-analyze service tests."""

from __future__ import annotations

from app.core.config import Settings
from app.services.analyze import (
    LlmStubAnalyzer,
    MeetingAnalysisService,
    MockAnalyzer,
    build_analyzer,
)


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


def test_mock_extracts_decisions_and_actions() -> None:
    transcript = (
        "Toplantıya başlandı. Bütçe artışı kararlaştırıldı. "
        "Ali raporu hazırlayacak. Sıradaki gündeme geçildi."
    )
    out = MockAnalyzer(_settings()).analyze(transcript)
    assert "Toplantıya başlandı" in out.summary
    assert any("kararlaştırıldı" in d for d in out.decisions)
    assert any("hazırlayacak" in a.text for a in out.action_items)


def test_service_redacts_before_analysis() -> None:
    # PII in the transcript must not survive into the analyzer output.
    transcript = "Ali ali@example.com adresinden raporu gönderecek. Bütçe kararlaştırıldı."
    svc = MeetingAnalysisService(_settings(redact_pii=True))
    result = svc.analyze(transcript)
    assert result.redacted is True
    assert result.redaction_count >= 1
    blob = result.summary + " ".join(a.text for a in result.action_items)
    assert "ali@example.com" not in blob


def test_service_redaction_can_be_disabled() -> None:
    result = MeetingAnalysisService(_settings(redact_pii=False)).analyze("Merhaba dünya.")
    assert result.redacted is False
    assert result.redaction_count == 0


def test_build_analyzer_selects_backend() -> None:
    from app.services.analyze import OllamaAnalyzer
    assert isinstance(build_analyzer(_settings(backend="mock")), MockAnalyzer)
    assert isinstance(build_analyzer(_settings(backend="anthropic")), LlmStubAnalyzer)
    assert isinstance(build_analyzer(_settings(backend="ollama")), OllamaAnalyzer)


def test_llm_stub_raises() -> None:
    diar = LlmStubAnalyzer(_settings(backend="openai"))
    assert diar.model_loaded is False
    try:
        diar.analyze("metin")
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError")
