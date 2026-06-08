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
