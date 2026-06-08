from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def reset_singletons() -> Generator[None, None, None]:
    from app.core import config
    from app.services import transcribe

    config._settings = None
    transcribe._transcriber = None
    yield
    config._settings = None
    transcribe._transcriber = None
