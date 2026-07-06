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


def test_compute_der_preserves_exact_overlap_speakers() -> None:
    pytest.importorskip("pyannote.metrics")
    # Two speakers talk over the SAME [0,2) window (exact overlap). Building the
    # reference Annotation WITHOUT a unique track per turn would let the 2nd speaker
    # overwrite the 1st (only one survives) → a hyp detecting just one would score
    # DER ~0, hiding the miss. With per-turn tracks both ref speakers survive, so a
    # hyp missing one is ~half the reference speech missed (review #189).
    ref = [
        diar_matrix.Turn(0.0, 2.0, "SPEAKER_00"),
        diar_matrix.Turn(0.0, 2.0, "SPEAKER_01"),  # exact overlap, distinct speaker
    ]
    hyp = [diar_matrix.Turn(0.0, 2.0, "SPEAKER_00")]  # detects only one of the two
    der = diar_matrix.compute_der(ref, hyp, collar=0.0, skip_overlap=False)
    assert der == pytest.approx(0.5, abs=0.1)  # ~half the reference speech missed


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


class _FakeRevision:
    def __init__(self, commit_hash: str, refs: set[str], last_modified: float) -> None:
        self.commit_hash = commit_hash
        self.refs = refs
        self.last_modified = last_modified


class _FakeRepo:
    def __init__(self, repo_id: str, revisions: list[_FakeRevision]) -> None:
        self.repo_id = repo_id
        self.revisions = revisions


class _FakeCacheInfo:
    def __init__(self, repos: list[_FakeRepo]) -> None:
        self.repos = repos


def _install_fake_huggingface_hub(
    monkeypatch: pytest.MonkeyPatch,
    default_cache: _FakeCacheInfo,
    by_cache_dir: dict[str, _FakeCacheInfo] | None = None,
) -> None:
    import types

    by_cache_dir = by_cache_dir or {}

    def _fake_scan_cache_dir(cache_dir: str | None = None) -> _FakeCacheInfo:
        if cache_dir is None:
            return default_cache
        return by_cache_dir.get(cache_dir, _FakeCacheInfo([]))

    fake_module = types.ModuleType("huggingface_hub")
    fake_module.scan_cache_dir = _fake_scan_cache_dir  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)


def test_resolved_model_revision_prefers_main_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    # #235 (Codex review): decision evidence must carry an immutable snapshot
    # id even when --revision was not explicitly passed. When several cached
    # revisions exist for the same repo, "main" is the one actually resolved
    # by a bare model-id load (no revision kwarg), so it must win regardless
    # of last_modified ordering.
    cache = _FakeCacheInfo(
        [
            _FakeRepo(
                "pyannote/speaker-diarization-3.1",
                [
                    _FakeRevision("older111", {"main"}, last_modified=1.0),
                    _FakeRevision("newer222", set(), last_modified=2.0),
                ],
            )
        ]
    )
    _install_fake_huggingface_hub(monkeypatch, cache)
    assert diar_matrix.resolved_model_revision("pyannote/speaker-diarization-3.1") == "older111"


def test_resolved_model_revision_falls_back_to_most_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No revision has "main" in its refs (e.g. loaded by a pinned tag) — fall
    # back to the most recently used cached revision rather than returning
    # nothing.
    cache = _FakeCacheInfo(
        [
            _FakeRepo(
                "speechbrain/spkrec-ecapa-voxceleb",
                [
                    _FakeRevision("stale333", {"v1.0"}, last_modified=1.0),
                    _FakeRevision("fresh444", {"v2.0"}, last_modified=5.0),
                ],
            )
        ]
    )
    _install_fake_huggingface_hub(monkeypatch, cache)
    assert diar_matrix.resolved_model_revision("speechbrain/spkrec-ecapa-voxceleb") == "fresh444"


def test_resolved_model_revision_returns_none_when_repo_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_huggingface_hub(monkeypatch, _FakeCacheInfo([]))
    monkeypatch.setattr(diar_matrix, "_pyannote_cache_dir", lambda: "/fake/pyannote/cache")
    assert diar_matrix.resolved_model_revision("pyannote/speaker-diarization-3.1") is None


def test_resolved_model_revision_falls_back_to_pyannote_cache_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #235 GPU-host verification: pyannote.audio caches under
    # `<torch.hub dir>/pyannote`, NOT the standard `~/.cache/huggingface/hub`
    # scan_cache_dir() reads by default -- SpeechBrain resolved correctly on
    # the first pass, pyannote stayed null until this fallback was added.
    pyannote_cache_path = "/fake/torch/pyannote"
    monkeypatch.setattr(diar_matrix, "_pyannote_cache_dir", lambda: pyannote_cache_path)
    default_cache = _FakeCacheInfo([])  # nothing in the standard HF hub cache
    pyannote_cache = _FakeCacheInfo(
        [
            _FakeRepo(
                "pyannote/speaker-diarization-3.1",
                [_FakeRevision("84fd259124", {"main"}, last_modified=1.0)],
            )
        ]
    )
    _install_fake_huggingface_hub(monkeypatch, default_cache, {pyannote_cache_path: pyannote_cache})
    assert diar_matrix.resolved_model_revision("pyannote/speaker-diarization-3.1") == "84fd259124"


def test_resolved_model_revision_prefers_requested_revision_over_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #235 re-review (P2): an explicit --revision pin was silently resolved to
    # whatever cached revision happened to carry the "main" ref, instead of
    # the actually-requested pin, when both were present in the cache (e.g. a
    # prior unpinned run already cached "main", then a later run explicitly
    # pinned an older tag/commit). The requested revision must win.
    cache = _FakeCacheInfo(
        [
            _FakeRepo(
                "pyannote/speaker-diarization-3.1",
                [
                    _FakeRevision("mainhash00", {"main"}, last_modified=5.0),
                    _FakeRevision("pinnedhash1", {"v2.1"}, last_modified=1.0),
                ],
            )
        ]
    )
    _install_fake_huggingface_hub(monkeypatch, cache)
    assert (
        diar_matrix.resolved_model_revision(
            "pyannote/speaker-diarization-3.1", requested_revision="v2.1"
        )
        == "pinnedhash1"
    )
    assert (
        diar_matrix.resolved_model_revision(
            "pyannote/speaker-diarization-3.1", requested_revision="pinnedhash1"
        )
        == "pinnedhash1"
    )


def test_resolved_model_revision_prefers_requested_revision_across_cache_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #235 re-review (F4): the first fix only searched each cache root in
    # isolation, so a repo found in the DEFAULT HF cache with only "main"
    # cached there fell back to that "main" hash and returned immediately —
    # never checking the pyannote-specific root where the actually-requested
    # revision was cached. Codex reproduced this with exactly this two-root
    # shape. The requested revision must win regardless of which root has it.
    pyannote_cache_path = "/fake/torch/pyannote"
    monkeypatch.setattr(diar_matrix, "_pyannote_cache_dir", lambda: pyannote_cache_path)
    default_cache = _FakeCacheInfo(
        [
            _FakeRepo(
                "pyannote/speaker-diarization-3.1",
                [_FakeRevision("mainhash00", {"main"}, last_modified=5.0)],
            )
        ]
    )
    pyannote_cache = _FakeCacheInfo(
        [
            _FakeRepo(
                "pyannote/speaker-diarization-3.1",
                [_FakeRevision("pinnedhash1", {"v2.1"}, last_modified=1.0)],
            )
        ]
    )
    _install_fake_huggingface_hub(monkeypatch, default_cache, {pyannote_cache_path: pyannote_cache})
    assert (
        diar_matrix.resolved_model_revision(
            "pyannote/speaker-diarization-3.1", requested_revision="v2.1"
        )
        == "pinnedhash1"
    )
