"""make_synthetic_diar.py tests — deterministic conversation + exact RTTM."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


msd = _load("make_synthetic_diar")
diar_matrix = _load("diar_matrix")


def _clip(seconds: float, rate: int = 16000) -> np.ndarray:
    return np.ones(int(seconds * rate), dtype=np.float32)


def test_conversation_is_deterministic() -> None:
    clips = [_clip(1.0), _clip(2.0), _clip(0.5)]
    a1, r1 = msd.build_conversation(clips, 16000, turns=9, gap_sec=0.4, seed=7)
    a2, r2 = msd.build_conversation(clips, 16000, turns=9, gap_sec=0.4, seed=7)
    assert r1 == r2 and a1.shape == a2.shape  # same seed → identical


def test_rttm_turns_match_audio_and_roundtrip(tmp_path: Path) -> None:
    clips = [_clip(1.0), _clip(2.0)]
    audio, turns = msd.build_conversation(clips, 16000, turns=4, gap_sec=0.5, seed=3)
    assert len(turns) == 4
    # turns reference only the two speakers we provided
    assert {spk for _, _, spk in turns} <= {"SPEAKER_00", "SPEAKER_01"}
    # write RTTM and re-read with diar_matrix.load_rttm (cross-tool contract)
    rttm = tmp_path / "c.rttm"
    msd.write_rttm(rttm, "c", turns)
    parsed = diar_matrix.load_rttm(rttm)
    assert len(parsed) == 4
    assert parsed[0].speaker in {"SPEAKER_00", "SPEAKER_01"}
    # ground-truth end of last turn must not exceed audio length
    assert parsed[-1].end <= len(audio) / 16000 + 1e-6


def test_zero_der_against_self(tmp_path: Path) -> None:
    import pytest

    pytest.importorskip("pyannote.metrics")
    clips = [_clip(1.0), _clip(1.5), _clip(0.8)]
    _, turns = msd.build_conversation(clips, 16000, turns=6, gap_sec=0.3, seed=1)
    ref = [diar_matrix.Turn(s, s + d, spk) for s, d, spk in turns]
    # hypothesis == reference → DER must be 0 (sanity of the whole pipeline)
    assert diar_matrix.compute_der(ref, ref) == 0.0
