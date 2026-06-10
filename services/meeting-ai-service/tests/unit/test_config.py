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
