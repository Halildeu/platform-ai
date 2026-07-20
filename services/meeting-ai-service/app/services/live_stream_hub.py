"""In-memory per-meeting pub/sub hub for Faz 24 live analysis SSE relay.

Design (Zeynep 2026-07-20 kapsam kararı — canlı özet/karar/aksiyon):

`/analyze/live` produces incremental analysis for an in-progress meeting.
Clients (the desktop panel today; the mobile app later) subscribe to
`/analyze/live/stream/{meeting_id}` and receive each new result over an
HTTP SSE (Server-Sent Events) stream — no persistence hop, no meeting-service
round-trip. Persistence for partials is a follow-up (mimari uzatma:
capability binding + tombstone genişletmesi); the ephemeral path is enough
for the live-viewing use case.

Why in-memory + single process:

- meeting-ai-service today is a single replica per k3d cluster (deployment.yaml
  sets replicas=1). Any cross-instance fan-out (Redis Streams, NATS, etc.)
  would add a dep that is not yet justified.
- The upgrade path is well-known: when we scale out, swap this hub for a
  Redis-backed one behind the same `LiveStreamHub` interface. Subscribers /
  publishers do not change. Documented here so the transition is explicit,
  not a scramble.

Backpressure:

- Each subscriber owns a bounded `asyncio.Queue` (default 100 items).
- On `publish` the hub delivers non-blocking (`put_nowait`); if a subscriber's
  queue is full we DROP THE OLDEST item and enqueue the new one, then
  increment a `dropped_total` metric. This is the correct choice for live
  view: a stalled subscriber must not slow down the publisher (analyzer),
  and the freshest analysis is always more useful than a stale one.
- A dropped item does NOT crash the subscriber; the SSE loop keeps
  streaming the newer items.

Backwards-compatibility guard on `publish`:

- `publish` MUST NOT raise if a subscriber goes away between the snapshot
  and the enqueue — the caller (analyze_live_endpoint) is a request-handling
  path and cannot tolerate broadcast errors bubbling into the HTTP response.
- Errors during enqueue are counted (`dropped_total`) and swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class Subscriber:
    """One connected SSE reader.

    `connected_at` is used by the health/metrics view to surface long-lived
    subscribers; `queue` carries the JSON-serialisable analysis payloads.

    `eq=False` keeps the default `object.__hash__` (id-based) so instances
    are usable as `set` members — the hub tracks subscribers in a `set` for
    O(1) add/remove even when many subscribers connect to one meeting.
    """

    queue: asyncio.Queue[dict[str, Any]]
    connected_at: float = field(default_factory=time.monotonic)


class LiveStreamHub:
    """Per-meeting pub/sub hub with bounded, drop-oldest subscriber queues.

    The hub is intentionally tiny and dependency-free (stdlib only). A single
    `asyncio.Lock` guards the `meeting_id -> subscribers` dict; publish walks
    a snapshot of the subscriber set outside the lock so a slow subscriber
    cannot block other publishers.
    """

    DEFAULT_MAX_QUEUE_SIZE = 100

    def __init__(self, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        self._max_queue_size = max_queue_size
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    async def subscribe(self, meeting_id: str) -> Subscriber:
        """Register a new subscriber for `meeting_id`. Returns its queue handle."""
        sub = Subscriber(queue=asyncio.Queue(maxsize=self._max_queue_size))
        async with self._lock:
            self._subs[meeting_id].add(sub)
        return sub

    async def unsubscribe(self, meeting_id: str, sub: Subscriber) -> None:
        """Remove a subscriber. Safe to call twice; missing sub is a no-op."""
        async with self._lock:
            bucket = self._subs.get(meeting_id)
            if bucket is None:
                return
            bucket.discard(sub)
            if not bucket:
                # Free the dict entry so an unbounded set of meeting IDs
                # over the lifetime of the process does not leak memory.
                self._subs.pop(meeting_id, None)

    async def publish(self, meeting_id: str, event: dict[str, Any]) -> tuple[int, int]:
        """Deliver `event` to every subscriber of `meeting_id`.

        Returns `(delivered, dropped)`:
          - `delivered`: subscribers that received the event (either into a
            free slot or after evicting an older one).
          - `dropped`: subscribers whose queue was full AND whose oldest item
            could not be evicted (extremely rare — only if the queue is
            concurrently drained and then re-filled between our `get_nowait`
            and `put_nowait`).

        NEVER raises. A missing meeting_id delivers to zero subscribers.
        """
        async with self._lock:
            snapshot = list(self._subs.get(meeting_id, ()))

        if not snapshot:
            return (0, 0)

        delivered = 0
        dropped = 0
        for sub in snapshot:
            try:
                sub.queue.put_nowait(event)
                delivered += 1
                continue
            except asyncio.QueueFull:
                pass
            # Drop-oldest fallback: evict one and retry once. QueueEmpty is
            # a benign race — a real consumer drained the queue between the
            # QueueFull above and this get_nowait; the retry below still
            # enqueues the new event.
            with contextlib.suppress(asyncio.QueueEmpty):
                sub.queue.get_nowait()
            try:
                sub.queue.put_nowait(event)
                delivered += 1
                dropped += 1
            except asyncio.QueueFull:
                # Very rare race: queue re-filled between the evict and the
                # retry. Count as dropped and move on; the next `publish`
                # from the analyzer will carry a fresher payload anyway.
                dropped += 1

        return (delivered, dropped)

    async def subscriber_count(self, meeting_id: str) -> int:
        """Number of currently connected subscribers for a meeting."""
        async with self._lock:
            return len(self._subs.get(meeting_id, ()))

    async def total_subscribers(self) -> int:
        """Total connected subscribers across ALL meetings."""
        async with self._lock:
            return sum(len(v) for v in self._subs.values())
