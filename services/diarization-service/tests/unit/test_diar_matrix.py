"""diar_matrix.py tests — RTTM parse + DER + speechbrain VAD/clustering logic.

The pyannote / speechbrain MODEL paths need a GPU host and are not exercised
here; only the pure, deterministic helpers (RTTM parse, DER, energy VAD,
agglomerative clustering) are unit-tested — they carry the diarization logic
that must be correct regardless of which heavy backend is plugged in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
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


# --- speechbrain alternative: pure VAD + clustering logic (CPU, no model) --- #


def _tone(seconds: float, rate: int = 16000, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_energy_vad_finds_two_speech_islands() -> None:
    rate = 16000
    silence = np.zeros(int(0.5 * rate), dtype=np.float32)
    samples = np.concatenate([silence, _tone(0.5), silence, _tone(0.5), silence])
    segs = diar_matrix.energy_vad(samples, rate)
    assert len(segs) == 2
    # first island ~[0.5, 1.0], second ~[1.5, 2.0]; tolerate frame quantization
    assert segs[0][0] == pytest.approx(0.5, abs=0.1)
    assert segs[1][0] == pytest.approx(1.5, abs=0.1)
    # segments are ordered and non-overlapping
    assert segs[0][1] <= segs[1][0]


def test_energy_vad_empty_and_silence_return_no_segments() -> None:
    assert diar_matrix.energy_vad(np.zeros(0, dtype=np.float32), 16000) == []
    assert diar_matrix.energy_vad(np.zeros(16000, dtype=np.float32), 16000) == []


def test_agglomerative_labels_two_clusters_by_first_appearance() -> None:
    # two tight groups along orthogonal axes
    embs = np.array([[1.0, 0.0], [0.98, 0.02], [0.0, 1.0], [0.02, 0.98]])
    assert diar_matrix.agglomerative_labels(embs, num_speakers=2) == [0, 0, 1, 1]


def test_agglomerative_labels_auto_threshold_splits_clear_groups() -> None:
    embs = np.array([[1.0, 0.0], [0.99, 0.01], [0.0, 1.0], [0.01, 0.99]])
    labels = diar_matrix.agglomerative_labels(embs, num_speakers=0, distance_threshold=0.5)
    assert labels == [0, 0, 1, 1]


def test_agglomerative_labels_edge_cases() -> None:
    assert diar_matrix.agglomerative_labels(np.zeros((0, 2))) == []
    assert diar_matrix.agglomerative_labels(np.array([[1.0, 2.0]])) == [0]
