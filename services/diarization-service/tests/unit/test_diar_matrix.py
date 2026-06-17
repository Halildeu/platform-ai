"""diar_matrix.py tests — RTTM parse + DER (pyannote optional)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ is not a package; load diar_matrix.py by path.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "diar_matrix.py"
_spec = importlib.util.spec_from_file_location("diar_matrix", _SCRIPT)
assert _spec and _spec.loader
diar_matrix = importlib.util.module_from_spec(_spec)
sys.modules["diar_matrix"] = diar_matrix
_spec.loader.exec_module(diar_matrix)


def test_load_rttm_parses_speaker_turns(tmp_path: Path) -> None:
    rttm = tmp_path / "m.rttm"
    rttm.write_text(
        "SPEAKER m 1 0.000 2.500 <NA> <NA> SPEAKER_00 <NA> <NA>\n"
        "SPEAKER m 1 2.500 1.500 <NA> <NA> SPEAKER_01 <NA> <NA>\n"
        "# comment line ignored\n",
        encoding="utf-8",
    )
    turns = diar_matrix.load_rttm(rttm)
    assert len(turns) == 2
    assert turns[0].speaker == "SPEAKER_00"
    assert turns[0].start == 0.0 and turns[0].end == 2.5
    assert turns[1].start == 2.5 and turns[1].end == 4.0


def test_load_rttm_skips_malformed_lines(tmp_path: Path) -> None:
    rttm = tmp_path / "m.rttm"
    rttm.write_text("garbage\nSPEAKER too few\n", encoding="utf-8")
    assert diar_matrix.load_rttm(rttm) == []


def test_compute_der_identical_is_zero() -> None:
    pytest.importorskip("pyannote.metrics")
    ref = [diar_matrix.Turn(0.0, 2.0, "SPEAKER_00"), diar_matrix.Turn(2.0, 4.0, "SPEAKER_01")]
    # Same turns, different label numbering → optimal mapping → DER 0.
    hyp = [diar_matrix.Turn(0.0, 2.0, "SPEAKER_09"), diar_matrix.Turn(2.0, 4.0, "SPEAKER_07")]
    assert diar_matrix.compute_der(ref, hyp) == pytest.approx(0.0, abs=1e-6)


def test_compute_der_total_miss_is_one() -> None:
    pytest.importorskip("pyannote.metrics")
    ref = [diar_matrix.Turn(0.0, 4.0, "SPEAKER_00")]
    hyp: list = []  # nothing detected → 100% miss → DER 1.0
    assert diar_matrix.compute_der(ref, hyp) == pytest.approx(1.0, abs=1e-6)
