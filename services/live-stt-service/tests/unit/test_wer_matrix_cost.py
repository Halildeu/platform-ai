"""Unit tests for wer_matrix's cost wiring (GPU-free, no faster_whisper).

wer_matrix imports faster_whisper lazily inside main(), so the module — and the
pure `_cost_per_audio_min` helper — import cleanly without a GPU (review #165:
the cost wiring had no test).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from wer_matrix import _cost_per_audio_min  # noqa: E402


def test_electricity_only_known_value() -> None:
    # 200 W at 2.5 ₺/kWh, RTF 0.0696 → 0.00058 ₺/audio-min (the PR's worked example)
    assert _cost_per_audio_min(0.0696, 200.0, 2.5, 0.0, 0.0) == 0.00058


def test_amortization_only_when_no_electricity() -> None:
    # hardware-only cost must NOT be dropped just because elec price is 0
    # 30000 ₺ / 10000 h = 3.0 ₺/h; RTF 0.5 → 120 audio-min/h → 0.025 ₺/min
    assert _cost_per_audio_min(0.5, 0.0, 0.0, 30000.0, 10000.0) == 0.025


def test_electricity_plus_amortization() -> None:
    # 200 W @ 2.5 = 0.5 ₺/h + 30000/10000 = 3.0 ₺/h → 3.5 ₺/h; RTF 0.5 → 120/h
    assert _cost_per_audio_min(0.5, 200.0, 2.5, 30000.0, 10000.0) == pytest.approx(
        3.5 / 120.0, abs=1e-5
    )


def test_no_cost_inputs_returns_none() -> None:
    assert _cost_per_audio_min(0.0696, 0.0, 0.0, 0.0, 0.0) is None


def test_price_without_power_returns_none() -> None:
    # elec price set but no power figure → incomplete → None (not a 200W default)
    assert _cost_per_audio_min(0.5, 0.0, 2.5, 0.0, 0.0) is None


def test_hw_without_horizon_returns_none() -> None:
    assert _cost_per_audio_min(0.5, 0.0, 0.0, 30000.0, 0.0) is None


def test_no_rtf_returns_none() -> None:
    assert _cost_per_audio_min(None, 200.0, 2.5, 0.0, 0.0) is None
    assert _cost_per_audio_min(0.0, 200.0, 2.5, 0.0, 0.0) is None
