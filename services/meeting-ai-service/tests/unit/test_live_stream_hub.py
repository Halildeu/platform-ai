"""Unit tests for LiveStreamHub — the in-memory pub/sub for Faz 24 SSE relay.

Covers:
  - subscribe / unsubscribe lifecycle + membership
  - publish with zero subscribers is a no-op that returns (0, 0)
  - publish delivers to every subscriber of a meeting_id
  - publish does NOT deliver across meeting_ids (isolation)
  - drop-oldest kicks in when a subscriber's queue is full
  - subscriber_count reflects add/remove
  - publish NEVER raises even under a concurrent race

Async tests use `asyncio.run(main())` inside a sync pytest function — this
project has no `pytest-asyncio` plugin and `--strict-markers` forbids
custom markers. Wrapping keeps tests dependency-free.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.live_stream_hub import LiveStreamHub, Subscriber


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_subscribe_and_publish_delivers_to_subscriber() -> None:
    async def main() -> None:
        hub = LiveStreamHub(max_queue_size=8)
        sub = await hub.subscribe("m-1")
        delivered, dropped = await hub.publish("m-1", {"seq": 1})
        assert delivered == 1
        assert dropped == 0
        assert sub.queue.qsize() == 1
        assert sub.queue.get_nowait() == {"seq": 1}

    _run(main())


def test_publish_without_subscribers_is_noop() -> None:
    async def main() -> None:
        hub = LiveStreamHub()
        delivered, dropped = await hub.publish("nobody", {"seq": 1})
        assert (delivered, dropped) == (0, 0)

    _run(main())


def test_publish_fans_out_to_all_subscribers_of_meeting() -> None:
    async def main() -> None:
        hub = LiveStreamHub(max_queue_size=4)
        subs = [await hub.subscribe("m-1") for _ in range(3)]
        delivered, dropped = await hub.publish("m-1", {"seq": 42})
        assert delivered == 3
        assert dropped == 0
        for s in subs:
            assert s.queue.get_nowait() == {"seq": 42}

    _run(main())


def test_publish_isolates_by_meeting_id() -> None:
    async def main() -> None:
        hub = LiveStreamHub()
        sub_a = await hub.subscribe("m-A")
        sub_b = await hub.subscribe("m-B")

        delivered_a, _ = await hub.publish("m-A", {"who": "A"})
        delivered_b, _ = await hub.publish("m-B", {"who": "B"})

        assert delivered_a == 1
        assert delivered_b == 1
        assert sub_a.queue.get_nowait() == {"who": "A"}
        assert sub_b.queue.get_nowait() == {"who": "B"}
        # Cross-check: A's queue was never touched by B's publish and vice versa.
        assert sub_a.queue.empty()
        assert sub_b.queue.empty()

    _run(main())


def test_drop_oldest_on_queue_full_still_delivers_new_event() -> None:
    async def main() -> None:
        hub = LiveStreamHub(max_queue_size=2)
        sub = await hub.subscribe("m-1")
        # Fill the queue to capacity.
        await hub.publish("m-1", {"seq": 1})
        await hub.publish("m-1", {"seq": 2})
        assert sub.queue.qsize() == 2

        # Third publish must evict the oldest (seq=1) and enqueue seq=3.
        delivered, dropped = await hub.publish("m-1", {"seq": 3})
        assert delivered == 1
        assert dropped == 1

        remaining = [sub.queue.get_nowait(), sub.queue.get_nowait()]
        # seq=1 evicted; queue holds seq=2 (survivor) then seq=3 (new).
        assert remaining == [{"seq": 2}, {"seq": 3}]

    _run(main())


def test_subscriber_count_reflects_add_and_remove() -> None:
    async def main() -> None:
        hub = LiveStreamHub()
        assert await hub.subscriber_count("m-1") == 0
        sub1 = await hub.subscribe("m-1")
        sub2 = await hub.subscribe("m-1")
        assert await hub.subscriber_count("m-1") == 2

        await hub.unsubscribe("m-1", sub1)
        assert await hub.subscriber_count("m-1") == 1

        await hub.unsubscribe("m-1", sub2)
        assert await hub.subscriber_count("m-1") == 0

        # Meeting bucket is freed when the last subscriber leaves — an internal
        # detail we still verify so a leak here surfaces early.
        assert "m-1" not in hub._subs

    _run(main())


def test_unsubscribe_missing_subscriber_is_safe() -> None:
    async def main() -> None:
        hub = LiveStreamHub()
        fake_sub = Subscriber(queue=asyncio.Queue())
        # Not raising is the guarantee — a double-unsubscribe or racing unsubscribe
        # must never crash the SSE finally block.
        await hub.unsubscribe("nobody", fake_sub)
        await hub.unsubscribe("nobody", fake_sub)

    _run(main())


def test_total_subscribers_aggregates_across_meetings() -> None:
    async def main() -> None:
        hub = LiveStreamHub()
        await hub.subscribe("m-A")
        await hub.subscribe("m-A")
        await hub.subscribe("m-B")
        assert await hub.total_subscribers() == 3

    _run(main())


def test_max_queue_size_validation() -> None:
    with pytest.raises(ValueError):
        LiveStreamHub(max_queue_size=0)


def test_publish_never_raises_even_on_transient_race() -> None:
    """`publish` swallows queue anomalies and returns a count instead of raising."""

    async def main() -> None:
        hub = LiveStreamHub(max_queue_size=1)
        sub = await hub.subscribe("m-1")

        async def consumer() -> None:
            for _ in range(50):
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0)

        async def publisher() -> None:
            for i in range(50):
                await hub.publish("m-1", {"seq": i})

        await asyncio.gather(consumer(), publisher())
        # If we got here without an exception, the guarantee holds. The exact
        # (delivered, dropped) split is nondeterministic under the race — we do
        # not assert on it beyond "no crash".

    _run(main())
