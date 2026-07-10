"""Config unit tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.backend == "mock"
    assert s.redact_pii is True
    assert s.request_timeout == 60


def test_backend_pattern_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        Settings(backend="gemini")


def test_request_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(request_timeout=0)


def test_redaction_cannot_be_disabled_on_real_backend() -> None:
    """Issue #49 KVKK boundary: redaction is mandatory for any non-mock backend."""
    for backend in ("anthropic", "openai", "ollama"):
        with pytest.raises(ValidationError):
            Settings(backend=backend, redact_pii=False)


def test_redaction_disable_allowed_only_on_mock() -> None:
    """Mock is in-process (no data leaves the service), so the dev switch is OK."""
    s = Settings(backend="mock", redact_pii=False)
    assert s.redact_pii is False
    # Real backends with redaction left on are fine too.
    assert Settings(backend="ollama", redact_pii=True).redact_pii is True


def test_ingestion_enabled_requires_credentials() -> None:
    """#244 AI-1: fail closed at startup, not on the first ingestion call."""
    with pytest.raises(ValidationError):
        Settings(ingestion_enabled=True)


def test_ingestion_enabled_with_full_credentials_is_valid() -> None:
    s = Settings(
        ingestion_enabled=True,
        meeting_service_token_url="http://keycloak/token",
        meeting_service_client_id="meeting-ai-service",
        meeting_service_client_secret="s3cr3t",
    )
    assert s.ingestion_enabled is True


def test_ingestion_disabled_does_not_require_credentials() -> None:
    assert Settings().ingestion_enabled is False


def test_effective_prompt_version_defaults_per_backend() -> None:
    assert Settings(backend="mock").effective_prompt_version == "mock-v1"
    assert Settings(backend="ollama").effective_prompt_version == "ollama-v1"


def test_effective_prompt_version_explicit_override_wins() -> None:
    s = Settings(backend="mock", prompt_version="custom-v3")
    assert s.effective_prompt_version == "custom-v3"
