"""Unit tests for the #42 GPU VRAM admission guard (resolve_worker_count).

Verifies the guard is OFF by default and on CPU, and that on CUDA with a
positive budget it clamps the worker count using the operator-supplied
per-worker figure (no hard-coded estimate). GPU-free.
"""

from __future__ import annotations

from app.core.config import Settings, resolve_worker_count


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


def test_guard_disabled_by_default() -> None:
    # default budget is 0 -> guard never engages, even on cuda
    plan = resolve_worker_count(_settings(device="cuda", worker_max_workers=4))
    assert plan.effective == 4
    assert plan.requested == 4
    assert plan.affordable is None
    assert plan.clamped is False


def test_guard_inactive_on_cpu() -> None:
    # budget set but device is cpu -> not a GPU concern, no clamp
    plan = resolve_worker_count(
        _settings(
            device="cpu",
            worker_max_workers=4,
            worker_vram_budget_mb=4200,
            worker_vram_per_worker_mb=2100,
        )
    )
    assert plan.effective == 4
    assert plan.clamped is False
    assert plan.affordable is None


def test_clamps_when_requested_exceeds_budget() -> None:
    # 6300 / 2100 = 3 affordable; requested 4 -> clamp to 3
    plan = resolve_worker_count(
        _settings(
            device="cuda",
            worker_max_workers=4,
            worker_vram_budget_mb=6300,
            worker_vram_per_worker_mb=2100,
        )
    )
    assert plan.affordable == 3
    assert plan.effective == 3
    assert plan.clamped is True


def test_no_clamp_when_within_budget() -> None:
    # 8400 / 2100 = 4 affordable; requested 2 -> unchanged
    plan = resolve_worker_count(
        _settings(
            device="cuda",
            worker_max_workers=2,
            worker_vram_budget_mb=8400,
            worker_vram_per_worker_mb=2100,
        )
    )
    assert plan.affordable == 4
    assert plan.effective == 2
    assert plan.clamped is False


def test_affordable_floor_is_one() -> None:
    # budget smaller than a single worker still allows 1 (never 0)
    plan = resolve_worker_count(
        _settings(
            device="cuda",
            worker_max_workers=3,
            worker_vram_budget_mb=1000,
            worker_vram_per_worker_mb=2100,
        )
    )
    assert plan.affordable == 1
    assert plan.effective == 1
    assert plan.clamped is True


def test_measured_rtx4070_budget_caps_at_three() -> None:
    # 8 GiB usable budget with measured ~2100 MiB/worker -> 3 max (matches #42)
    plan = resolve_worker_count(
        _settings(
            device="cuda",
            worker_max_workers=8,
            worker_vram_budget_mb=7800,
            worker_vram_per_worker_mb=2100,
        )
    )
    assert plan.effective == 3
    assert plan.clamped is True
