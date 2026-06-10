from __future__ import annotations

import pytest

from app.core.config import Settings


def test_provisional_gpu_defaults() -> None:
    settings = Settings()
    assert settings.model_name == "large-v3"
    assert settings.device == "cuda"
    assert settings.compute_type == "float16"
    assert settings.chunk_min_sec == 10.0
    assert settings.chunk_max_sec == 15.0
    assert settings.redis_enabled is False


def test_chunk_bounds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="chunk_min_sec"):
        Settings(chunk_min_sec=15, chunk_max_sec=10)


def test_model_hash_required_for_production() -> None:
    with pytest.raises(ValueError, match="model_sha256"):
        Settings(environment="production", model_sha256="")
    with pytest.raises(ValueError, match="model_path"):
        Settings(environment="production", model_sha256="sha256:test")
    assert Settings(
        environment="production",
        model_sha256="sha256:test",
        model_path="/models/large-v3",
    ).model_sha256
