"""Shared test fixtures: reset module singletons so each test is isolated."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import app.core.config as config_mod
import app.services.analyze as analyze_mod


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    config_mod._settings = None
    analyze_mod._service = None
    yield
    config_mod._settings = None
    analyze_mod._service = None
