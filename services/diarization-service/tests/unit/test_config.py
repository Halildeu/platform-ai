"""Config unit tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.backend == "mock"
    assert s.device == "cpu"
    assert s.mock_num_speakers == 2
    assert s.request_timeout == 60


def test_backend_pattern_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        Settings(backend="banana")


def test_request_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(request_timeout=301)
