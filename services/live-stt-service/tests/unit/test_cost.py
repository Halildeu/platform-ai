"""Unit tests for the #43 parametric cost model (GPU-free)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import cost  # noqa: E402


def test_local_cost_electricity_only() -> None:
    # 140 W at 3.0 /kWh -> 0.42 /h, no amortization
    r = cost.local_cost_per_hour(power_w=140, electricity_price_per_kwh=3.0)
    assert r["electricity"] == pytest.approx(0.42)
    assert r["amortization"] == 0.0
    assert r["total"] == pytest.approx(0.42)


def test_local_cost_with_amortization() -> None:
    # 30000 hardware over 10000 h -> 3.0 /h amortization
    r = cost.local_cost_per_hour(
        power_w=140,
        electricity_price_per_kwh=3.0,
        hardware_cost=30000,
        amortization_hours=10000,
    )
    assert r["amortization"] == pytest.approx(3.0)
    assert r["total"] == pytest.approx(3.42)


def test_local_cost_rejects_negative() -> None:
    with pytest.raises(ValueError):
        cost.local_cost_per_hour(power_w=-1, electricity_price_per_kwh=3.0)


def test_audio_minutes_per_wall_hour() -> None:
    # RTF 0.5 (2x faster than realtime), 1 stream -> 120 audio-min/h
    assert cost.audio_minutes_per_wall_hour(0.5, 1) == pytest.approx(120.0)
    # 2 concurrent streams -> 240
    assert cost.audio_minutes_per_wall_hour(0.5, 2) == pytest.approx(240.0)


def test_audio_minutes_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        cost.audio_minutes_per_wall_hour(0.0, 1)
    with pytest.raises(ValueError):
        cost.audio_minutes_per_wall_hour(0.5, 0)


def test_cost_per_audio_minute() -> None:
    assert cost.cost_per_audio_minute(3.42, 120.0) == pytest.approx(0.0285)
    assert cost.cost_per_audio_minute(3.42, 0.0) == 0.0


def test_compare_local_cheaper() -> None:
    r = cost.compare_local_vs_cloud(
        local_cost_per_hour_total=3.42,
        cloud_cost_per_hour=20.0,
        audio_min_per_wall_hour=120.0,
    )
    assert r["cheaper"] == "local"
    assert r["delta_per_audio_min"] == pytest.approx(20.0 / 120 - 3.42 / 120)


def test_compare_cloud_cheaper() -> None:
    r = cost.compare_local_vs_cloud(
        local_cost_per_hour_total=50.0,
        cloud_cost_per_hour=20.0,
        audio_min_per_wall_hour=120.0,
    )
    assert r["cheaper"] == "cloud"
