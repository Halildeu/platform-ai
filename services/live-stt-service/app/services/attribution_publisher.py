"""Redis Streams publisher for directSttSessionAttribution.v1 (#3746 AI-D3).

Metadata-only delivery on the existing Redis plane: the payload carries
per-window labels and milliseconds — never audio, never transcript text (the
job schema-validates that shape before this class ever sees it). Bounded
producer-side trim keeps an unconsumed stream from growing without limit.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

__all__ = ["RedisAttributionPublisher"]


class _RedisLike(Protocol):  # pragma: no cover - protocol
    def xadd(
        self,
        name: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> Any: ...


class RedisAttributionPublisher:
    """XADD one event per finished session; raises on delivery failure so the
    job reports FAILED rather than pretending the event went out."""

    def __init__(self, redis_client: _RedisLike, stream: str, *, maxlen: int = 10_000) -> None:
        if not stream:
            raise ValueError("stream name is required")
        if maxlen < 100:
            raise ValueError("maxlen must be >= 100")
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen

    def publish(self, payload: dict[str, Any]) -> None:
        self._redis.xadd(
            self._stream,
            {
                "schema": str(payload.get("schema", "")),
                "meetingId": str(payload.get("meetingId", "")),
                "payload": json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
            },
            maxlen=self._maxlen,
            approximate=True,
        )
