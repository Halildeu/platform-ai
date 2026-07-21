"""Thread-safe startup state for the two streaming Whisper models."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

PreloadRoleStatus = Literal["disabled", "pending", "loading", "ready", "failed", "stopping"]


@dataclass(frozen=True)
class StreamingPreloadSnapshot:
    enabled: bool
    status: Literal["disabled", "loading", "ready", "failed", "stopping"]
    roles: dict[str, PreloadRoleStatus]
    attempts: dict[str, int]

    @property
    def ready(self) -> bool:
        return self.status == "ready"


class StreamingPreloadState:
    """Own preload readiness and cancellation without exposing error details."""

    _ROLES = ("live", "final")

    def __init__(self, *, enabled: bool) -> None:
        initial: PreloadRoleStatus = "pending" if enabled else "disabled"
        self._enabled = enabled
        self._roles = {role: initial for role in self._ROLES}
        self._attempts = {role: 0 for role in self._ROLES}
        self._lock = threading.Lock()
        self.stop_event = threading.Event()

    def begin_attempt(self, role: str) -> None:
        with self._lock:
            if self.stop_event.is_set():
                self._roles[role] = "stopping"
                return
            self._attempts[role] += 1
            self._roles[role] = "loading"

    def mark_ready(self, role: str) -> None:
        with self._lock:
            self._roles[role] = "stopping" if self.stop_event.is_set() else "ready"

    def mark_failed(self, role: str) -> None:
        with self._lock:
            self._roles[role] = "stopping" if self.stop_event.is_set() else "failed"

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            for role, status in self._roles.items():
                if status != "ready":
                    self._roles[role] = "stopping"

    def snapshot(self) -> StreamingPreloadSnapshot:
        with self._lock:
            roles = dict(self._roles)
            attempts = dict(self._attempts)
        if not self._enabled:
            status: Literal["disabled", "loading", "ready", "failed", "stopping"] = "disabled"
        elif self.stop_event.is_set():
            status = "stopping"
        elif all(value == "ready" for value in roles.values()):
            status = "ready"
        elif any(value == "failed" for value in roles.values()):
            status = "failed"
        else:
            status = "loading"
        return StreamingPreloadSnapshot(
            enabled=self._enabled,
            status=status,
            roles=roles,
            attempts=attempts,
        )
