"""#3746 AI-D2 — bounded transient session audio store unit tests.

Contract under test (design doc docs/faz-24-scheduled-diarization-design-3746.md, D2):
RAM-only whole-session PCM16 accumulation; cap overflow cancels the whole
session (fail-closed: whole-session audio or nothing); finish hands audio out
exactly once; TTL sweep drops idle sessions; the concurrent-session bound
rejects new keys without touching existing ones.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.session_audio_store import (
    AppendOutcome,
    SessionAudio,
    SessionAudioStore,
    StoreStats,
)

SECOND = 16_000 * 2  # one second of PCM16 mono 16 kHz


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


def make_store(
    *,
    cap_bytes: int = 10 * SECOND,
    idle_ttl_sec: float = 60.0,
    max_sessions: int = 2,
    clock: FakeClock | None = None,
) -> tuple[SessionAudioStore, FakeClock]:
    clock = clock or FakeClock()
    store = SessionAudioStore(
        cap_bytes=cap_bytes,
        idle_ttl_sec=idle_ttl_sec,
        max_sessions=max_sessions,
        clock=clock,
    )
    return store, clock


def test_append_then_finish_returns_concatenated_audio_once() -> None:
    store, _ = make_store()
    assert store.append("s1", b"aa") is AppendOutcome.ACCEPTED
    assert store.append("s1", b"bb") is AppendOutcome.ACCEPTED
    assert store.finish("s1") == SessionAudio(pcm16=b"aabb", windows=())
    # Exactly once: the session is gone afterwards.
    assert store.finish("s1") is None


def test_cap_overflow_cancels_whole_session_and_stays_cancelled() -> None:
    store, _ = make_store(cap_bytes=SECOND)
    assert store.append("s1", b"x" * (SECOND - 1)) is AppendOutcome.ACCEPTED
    # This append crosses the cap: everything is dropped, not truncated.
    assert store.append("s1", b"xx") is AppendOutcome.CANCELLED_CAP
    assert store.stats().total_bytes == 0
    # Later appends keep reporting the cancellation without storing anything.
    assert store.append("s1", b"y") is AppendOutcome.CANCELLED_CAP
    assert store.finish("s1") is None
    stats = store.stats()
    assert stats.cancelled_cap_total == 1
    assert stats.finished_total == 0
    # The tombstone was consumed by finish; the key is reusable afterwards.
    assert store.append("s1", b"z") is AppendOutcome.ACCEPTED
    assert store.finish("s1").pcm16 == b"z"


def test_single_oversized_append_cancels() -> None:
    store, _ = make_store(cap_bytes=SECOND)
    assert store.append("s1", b"x" * (SECOND + 1)) is AppendOutcome.CANCELLED_CAP
    assert store.finish("s1") is None


def test_max_sessions_rejects_new_keys_only() -> None:
    store, _ = make_store(max_sessions=2)
    assert store.append("s1", b"a") is AppendOutcome.ACCEPTED
    assert store.append("s2", b"b") is AppendOutcome.ACCEPTED
    assert store.append("s3", b"c") is AppendOutcome.REJECTED_SESSIONS
    # Existing sessions are unaffected by the rejection.
    assert store.append("s1", b"a") is AppendOutcome.ACCEPTED
    assert store.finish("s3") is None
    # Freeing a slot admits the previously rejected key.
    assert store.finish("s2").pcm16 == b"b"
    assert store.append("s3", b"c") is AppendOutcome.ACCEPTED


def test_ttl_sweep_drops_only_idle_sessions() -> None:
    store, clock = make_store(idle_ttl_sec=60.0)
    store.append("idle", b"a")
    clock.advance(45.0)
    store.append("busy", b"b")
    clock.advance(30.0)  # idle at 75s > TTL, busy at 30s < TTL
    assert store.sweep() == 1
    assert store.finish("idle") is None
    assert store.finish("busy").pcm16 == b"b"
    assert store.stats().swept_total == 1


def test_empty_append_touches_idle_clock_without_storing() -> None:
    store, clock = make_store(idle_ttl_sec=60.0)
    store.append("s1", b"a")
    clock.advance(45.0)
    assert store.append("s1", b"") is AppendOutcome.ACCEPTED
    clock.advance(45.0)  # 90s since data, 45s since touch
    assert store.sweep() == 0
    assert store.finish("s1").pcm16 == b"a"


def test_cancel_and_clear_drop_audio() -> None:
    store, _ = make_store()
    store.append("s1", b"a")
    store.cancel("s1")
    assert store.finish("s1") is None
    store.append("s2", b"b")
    store.clear()
    assert store.finish("s2") is None
    assert store.stats() == StoreStats(
        sessions=0, total_bytes=0, cancelled_cap_total=0, swept_total=0, finished_total=0
    )


def test_key_validation() -> None:
    store, _ = make_store()
    with pytest.raises(ValueError):
        store.append("", b"a")
    with pytest.raises(ValueError):
        store.append("k" * 129, b"a")


def test_constructor_bounds() -> None:
    with pytest.raises(ValueError):
        SessionAudioStore(cap_bytes=100, idle_ttl_sec=60.0, max_sessions=1)
    with pytest.raises(ValueError):
        SessionAudioStore(cap_bytes=SECOND, idle_ttl_sec=0.0, max_sessions=1)
    with pytest.raises(ValueError):
        SessionAudioStore(cap_bytes=SECOND, idle_ttl_sec=60.0, max_sessions=0)


def test_settings_defaults_are_off_and_bounded() -> None:
    settings = Settings()
    assert settings.session_audio_store_enabled is False
    assert settings.session_audio_cap_bytes == 230_400_000
    assert settings.session_audio_idle_ttl_sec == 900.0
    assert settings.session_audio_max_sessions == 4


def test_window_map_tracks_time_ranges() -> None:
    store, _ = make_store()
    ms40 = b"x" * (40 * 32)  # 40 ms of PCM16 @ 16 kHz
    assert store.append("s1", ms40, window_seq=0) is AppendOutcome.ACCEPTED
    assert store.append("s1", b"y" * (25 * 32), window_seq=2) is AppendOutcome.ACCEPTED
    audio = store.finish("s1")
    assert audio is not None
    assert audio.windows == ((0, 0, 40), (2, 40, 65))
    assert len(audio.pcm16) == 65 * 32


def test_window_seq_must_be_unique_and_non_negative() -> None:
    store, _ = make_store()
    store.append("s1", b"x" * 32, window_seq=0)
    with pytest.raises(ValueError):
        store.append("s1", b"x" * 32, window_seq=0)
    with pytest.raises(ValueError):
        store.append("s1", b"x" * 32, window_seq=-1)


def test_cap_overflow_clears_window_map_too() -> None:
    store, _ = make_store(cap_bytes=SECOND)
    store.append("s1", b"x" * (SECOND - 1), window_seq=0)
    assert store.append("s1", b"xx", window_seq=1) is AppendOutcome.CANCELLED_CAP
    assert store.finish("s1") is None
