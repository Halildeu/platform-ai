"""Bounded transient session audio store (#3746 AI-D2, ADR-0033/ADR-0036).

Accumulates a session's PCM16 mono 16 kHz bytes in RAM so a post-session
diarization batch (ADR-0033: scheduled post-processing, never live/EOF) can
run on the whole session. ADR-0036 boundary: audio lives only in this
process's memory — it is never written to disk, logs, metrics or events, and
it leaves the store exactly once (``finish``) or is dropped (``cancel`` /
TTL sweep / cap overflow / shutdown).

Fail-closed semantics: a session that overflows its byte cap is cancelled —
its audio is dropped immediately and stays dropped (later appends are
rejected), so downstream can only ever see *whole-session* audio or nothing.
Partial audio would produce wrong speaker labels; "no attribution" is the
accepted degraded mode (design doc `docs/faz-24-scheduled-diarization-design-3746.md`,
D2).

The store is wiring-free by design: nothing in the request paths touches it
unless the (default-off) settings flag enables it and a later slice (AI-D3/
BE-D4) supplies frames and the finish signal. Observability is size/count
only — never content.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AppendOutcome",
    "SessionAudio",
    "SessionAudioStore",
    "StoreStats",
]

# One PCM16 mono 16 kHz second. The store speaks bytes, but this makes the
# defaults and tests legible.
_BYTES_PER_SECOND = 16_000 * 2

# Keys are caller-supplied opaque identifiers (gateway session key, D4). The
# store never parses them; it only bounds them so a buggy caller cannot grow
# unbounded dict keys.
_MAX_KEY_LENGTH = 128


class AppendOutcome(Enum):
    """Result of one append call."""

    ACCEPTED = "accepted"
    # The session hit its byte cap now or earlier: audio is gone, stays gone.
    CANCELLED_CAP = "cancelled_cap"
    # The store is at max_sessions and this key is new: nothing was stored.
    REJECTED_SESSIONS = "rejected_sessions"
    # The key was explicitly cancelled or already finished.
    REJECTED_CLOSED = "rejected_closed"


@dataclass(frozen=True)
class StoreStats:
    """Size/count facts only — safe for logs and metrics (no content)."""

    sessions: int
    total_bytes: int
    cancelled_cap_total: int
    swept_total: int
    finished_total: int


@dataclass(frozen=True)
class SessionAudio:
    """Whole-session audio plus the window time map (#3746 AI-D3).

    ``windows`` maps each appended window_seq to its [start_ms, end_ms) range
    on the session timeline, derived from byte offsets (32 bytes/ms for PCM16
    mono 16 kHz). Appends made without a window_seq (AI-D2 callers) simply do
    not appear in the map.
    """

    pcm16: bytes
    windows: tuple[tuple[int, int, int], ...]  # (window_seq, start_ms, end_ms)
    # start/end_ms live on the CONCATENATION timeline: windows are joined in
    # window_seq order at finish (forwards may arrive out of order), and a
    # dropped window is simply a gap in the map, never a time shift.


_BYTES_PER_MS = 32  # PCM16 mono 16 kHz


@dataclass
class _Session:
    # window_seq -> pcm bytes; joined in seq order at finish so out-of-order
    # arrival cannot scramble the audio timeline. Seq-less appends (legacy
    # AI-D2 callers) get increasing negative seqs starting far below zero:
    # arrival order is preserved among themselves, they sort before any real
    # window, and the map excludes them (seq < 0).
    chunks: dict[int, bytes]
    next_anonymous_seq: int
    total_bytes: int
    last_touched: float
    cancelled: bool


class SessionAudioStore:
    """Thread-safe, bounded, RAM-only per-session PCM accumulator.

    ``clock`` is injectable (monotonic seconds) so tests exercise TTL without
    sleeping. All methods are safe from any thread; the janitor loop in
    ``app.main`` calls :meth:`sweep` periodically when the store is enabled.
    """

    def __init__(
        self,
        *,
        cap_bytes: int,
        idle_ttl_sec: float,
        max_sessions: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cap_bytes < _BYTES_PER_SECOND:
            raise ValueError("cap_bytes must hold at least one second of PCM16 audio")
        if idle_ttl_sec <= 0:
            raise ValueError("idle_ttl_sec must be positive")
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._cap_bytes = cap_bytes
        self._idle_ttl_sec = idle_ttl_sec
        self._max_sessions = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._cancelled_cap_total = 0
        self._swept_total = 0
        self._finished_total = 0

    # ── writes ───────────────────────────────────────────────────────────

    def append(
        self, key: str, pcm16_bytes: bytes, *, window_seq: int | None = None
    ) -> AppendOutcome:
        """Add one window of PCM16 bytes to ``key``'s session.

        Empty payloads only refresh the idle clock. A cap overflow cancels the
        whole session *now* (audio freed under the lock) and every later
        append reports the same cancellation — the caller needs no state.
        ``window_seq`` orders this window on the session timeline (forwards
        may arrive out of order; finish joins in seq order). A duplicate seq
        with identical bytes is an idempotent no-op (retried forward); a
        duplicate with different bytes cancels the session — conflicting
        audio must never mint labels.
        """
        if not key or len(key) > _MAX_KEY_LENGTH:
            raise ValueError("session key must be 1..128 characters")
        now = self._clock()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                if len(self._sessions) >= self._max_sessions:
                    return AppendOutcome.REJECTED_SESSIONS
                session = _Session(
                    chunks={},
                    next_anonymous_seq=-(2**32),
                    total_bytes=0,
                    last_touched=now,
                    cancelled=False,
                )
                self._sessions[key] = session
            session.last_touched = now
            if session.cancelled:
                return AppendOutcome.CANCELLED_CAP
            if not pcm16_bytes:
                return AppendOutcome.ACCEPTED
            if window_seq is not None and window_seq < 0:
                raise ValueError("window_seq must be non-negative")
            if window_seq is not None and window_seq in session.chunks:
                if session.chunks[window_seq] == pcm16_bytes:
                    return AppendOutcome.ACCEPTED  # idempotent retry
                session.chunks = {}
                session.total_bytes = 0
                session.cancelled = True
                self._cancelled_cap_total += 1
                return AppendOutcome.CANCELLED_CAP
            if session.total_bytes + len(pcm16_bytes) > self._cap_bytes:
                # Fail closed: drop everything, remember only the tombstone.
                session.chunks = {}
                session.total_bytes = 0
                session.cancelled = True
                self._cancelled_cap_total += 1
                return AppendOutcome.CANCELLED_CAP
            if window_seq is None:
                window_seq = session.next_anonymous_seq
                session.next_anonymous_seq += 1
            session.chunks[window_seq] = pcm16_bytes
            session.total_bytes += len(pcm16_bytes)
            return AppendOutcome.ACCEPTED

    def finish(self, key: str) -> SessionAudio | None:
        """Hand the whole session's audio out exactly once and forget it.

        Returns ``None`` for unknown, cancelled or empty sessions — the
        caller must treat that as "no attribution", never retry with partial
        data. The cancelled tombstone is consumed here so the key can be
        reused by a later, unrelated session.
        """
        with self._lock:
            session = self._sessions.pop(key, None)
            if session is None or session.cancelled or session.total_bytes == 0:
                return None
            self._finished_total += 1
            ordered = sorted(session.chunks.items())
            payload = b"".join(chunk for _seq, chunk in ordered)
            windows: list[tuple[int, int, int]] = []
            offset = 0
            for seq, chunk in ordered:
                start_ms = offset // _BYTES_PER_MS
                end_ms = (offset + len(chunk)) // _BYTES_PER_MS
                if seq >= 0 and end_ms > start_ms:
                    windows.append((seq, start_ms, end_ms))
                offset += len(chunk)
            session.chunks = {}
            return SessionAudio(pcm16=payload, windows=tuple(windows))

    def cancel(self, key: str) -> None:
        """Drop a session's audio immediately (erasure, disconnect, abort)."""
        with self._lock:
            self._sessions.pop(key, None)

    def sweep(self) -> int:
        """Drop sessions idle past the TTL; returns how many were dropped."""
        now = self._clock()
        with self._lock:
            stale = [
                key
                for key, session in self._sessions.items()
                if now - session.last_touched > self._idle_ttl_sec
            ]
            for key in stale:
                del self._sessions[key]
            self._swept_total += len(stale)
            return len(stale)

    def clear(self) -> None:
        """Drop everything (shutdown path)."""
        with self._lock:
            self._sessions.clear()

    # ── reads ────────────────────────────────────────────────────────────

    def stats(self) -> StoreStats:
        with self._lock:
            return StoreStats(
                sessions=len(self._sessions),
                total_bytes=sum(s.total_bytes for s in self._sessions.values()),
                cancelled_cap_total=self._cancelled_cap_total,
                swept_total=self._swept_total,
                finished_total=self._finished_total,
            )
