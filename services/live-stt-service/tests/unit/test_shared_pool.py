"""#42 shared-model multi-stream backend — CI-safe wiring tests.

These assert the backend selection, supervisor lifecycle and stream-count
admission *without* loading faster-whisper (the supervisor only imports the
model on the first transcribe). Real GPU concurrency / VRAM / saturation is
measured separately on the RTX 4070 target as #42's measurement deliverable.
"""

from __future__ import annotations

from app.core.config import Settings, resolve_worker_count
from app.services.worker import (
    SharedModelWorkerPool,
    build_worker_pool,
)


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "worker_backend": "shared",
        "worker_max_workers": 2,
        "device": "cpu",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_config_accepts_shared_backend() -> None:
    assert _settings().worker_backend == "shared"


def test_build_worker_pool_returns_shared_pool() -> None:
    pool = build_worker_pool(_settings())
    try:
        assert isinstance(pool, SharedModelWorkerPool)
        # Model is loaded lazily on first transcribe, never at construction.
        assert pool.model_loaded is False
    finally:
        pool.close()


def test_shared_pool_stream_count_matches_workers() -> None:
    pool = build_worker_pool(_settings(worker_max_workers=3))
    try:
        assert pool._num_streams == 3  # type: ignore[attr-defined]
    finally:
        pool.close()


def test_shared_pool_respects_vram_admission_clamp() -> None:
    # device=cuda + budget enables the guard: 4096 / 2100 -> affordable 1.
    settings = _settings(
        worker_max_workers=4,
        device="cuda",
        worker_vram_budget_mb=4096,
        worker_vram_per_worker_mb=2100,
    )
    plan = resolve_worker_count(settings)
    assert plan.clamped is True
    assert plan.effective == 1


def test_shared_pool_supervisor_starts_and_stops() -> None:
    pool = build_worker_pool(_settings())
    try:
        assert pool._process is not None  # type: ignore[attr-defined]
        assert pool._process.is_alive() is True  # type: ignore[attr-defined]
    finally:
        pool.close()
    assert pool._process.is_alive() is False  # type: ignore[attr-defined]
