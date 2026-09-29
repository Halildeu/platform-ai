"""#3746 AI-D3 — window dominance + WAV framing unit tests.

Direction matters: these tests assert the fail-closed side (UU on contested,
UU on too-little speech, silence does not dilute dominance) — not merely that
some label comes out.
"""

from __future__ import annotations

import io
import wave

import pytest

from app.services.session_attribution import (
    DiarTurn,
    WindowAssignment,
    WindowSpan,
    assign_windows,
    pcm16_to_wav_bytes,
)


def test_single_speaker_window_is_dominant() -> None:
    windows = [WindowSpan(0, 0, 5_000)]
    turns = [DiarTurn("SPEAKER_00", 0, 4_800)]
    [a] = assign_windows(windows, turns)
    assert a == WindowAssignment(0, "SPEAKER_00", 0, 5_000, 1.0)


def test_silence_does_not_dilute_dominance() -> None:
    # 500 ms of one speaker in a 5 s window: share of SPEECH time is 1.0.
    windows = [WindowSpan(0, 0, 5_000)]
    turns = [DiarTurn("SPEAKER_01", 1_000, 1_500)]
    [a] = assign_windows(windows, turns)
    assert a.speaker == "SPEAKER_01"
    assert a.dominance_ratio == 1.0


def test_contested_window_is_uu_with_best_ratio() -> None:
    windows = [WindowSpan(3, 0, 6_000)]
    turns = [
        DiarTurn("SPEAKER_00", 0, 3_300),
        DiarTurn("SPEAKER_01", 3_300, 6_000),
    ]
    [a] = assign_windows(windows, turns)
    assert a.speaker == "UU"
    assert a.window_seq == 3
    assert a.dominance_ratio == pytest.approx(0.55)


def test_overlapping_turns_push_contested_windows_to_uu() -> None:
    # Simultaneous speech: both clusters cover the same span fully.
    windows = [WindowSpan(0, 0, 4_000)]
    turns = [
        DiarTurn("SPEAKER_00", 0, 4_000),
        DiarTurn("SPEAKER_01", 0, 4_000),
    ]
    [a] = assign_windows(windows, turns)
    assert a.speaker == "UU"
    assert a.dominance_ratio == pytest.approx(0.5)


def test_too_little_speech_is_uu_at_ratio_zero() -> None:
    windows = [WindowSpan(0, 0, 5_000)]
    turns = [DiarTurn("SPEAKER_00", 0, 200)]  # under min_speech_ms=250
    [a] = assign_windows(windows, turns)
    assert a == WindowAssignment(0, "UU", 0, 5_000, 0.0)


def test_silent_window_is_uu() -> None:
    windows = [WindowSpan(0, 0, 5_000), WindowSpan(1, 5_000, 10_000)]
    turns = [DiarTurn("SPEAKER_00", 0, 5_000)]
    first, second = assign_windows(windows, turns)
    assert first.speaker == "SPEAKER_00"
    assert second == WindowAssignment(1, "UU", 5_000, 10_000, 0.0)


def test_session_stable_labels_across_distant_windows() -> None:
    # One clustering pass: the same cluster label lands in windows far apart.
    windows = [WindowSpan(0, 0, 5_000), WindowSpan(7, 60_000, 65_000)]
    turns = [
        DiarTurn("SPEAKER_02", 0, 5_000),
        DiarTurn("SPEAKER_02", 60_000, 65_000),
    ]
    first, second = assign_windows(windows, turns)
    assert first.speaker == second.speaker == "SPEAKER_02"


def test_assignments_sorted_and_unique_window_seq_enforced() -> None:
    windows = [WindowSpan(2, 10_000, 15_000), WindowSpan(0, 0, 5_000)]
    turns = [DiarTurn("S1", 0, 15_000)]
    assert [a.window_seq for a in assign_windows(windows, turns)] == [0, 2]
    with pytest.raises(ValueError):
        assign_windows([WindowSpan(0, 0, 1_000), WindowSpan(0, 1_000, 2_000)], turns)


def test_threshold_bounds() -> None:
    with pytest.raises(ValueError):
        assign_windows([], [], dominance_threshold=0.0)
    with pytest.raises(ValueError):
        assign_windows([], [], dominance_threshold=1.1)
    with pytest.raises(ValueError):
        assign_windows([], [], min_speech_ms=-1)


def test_invalid_spans_and_turns_rejected() -> None:
    with pytest.raises(ValueError):
        WindowSpan(0, 1_000, 1_000)
    with pytest.raises(ValueError):
        DiarTurn("", 0, 1_000)
    with pytest.raises(ValueError):
        DiarTurn("S1", 500, 400)


def test_pcm16_to_wav_roundtrip_in_ram() -> None:
    pcm = bytes(range(64)) * 500  # 32 000 bytes = 1 s of PCM16 @ 16 kHz
    wav = pcm16_to_wav_bytes(pcm)
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 16_000
        assert reader.readframes(reader.getnframes()) == pcm


def test_pcm16_to_wav_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        pcm16_to_wav_bytes(b"")
    with pytest.raises(ValueError):
        pcm16_to_wav_bytes(b"abc")
    with pytest.raises(ValueError):
        pcm16_to_wav_bytes(b"ab", sample_rate=0)
