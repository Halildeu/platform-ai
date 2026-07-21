"""Startup preload of the streaming models (Faz 24 Bulgu 3-F).

Loading the models lazily on the first WebSocket deadlocked against client
patience: a cold load takes minutes, the desktop recorder waits 10s for
`ready`, and its disconnect cancelled the load ("WS disconnected during model
load"). Every attempt restarted from zero, so no session could ever start.

These tests pin the three properties that keep that deadlock impossible:
both models are loaded at boot, startup is not blocked while it happens, and a
preload failure degrades to the old lazy path instead of taking the service
down.
"""

from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _RecordingService:
    """Stands in for a Whisper service, recording ensure_model() calls."""

    def __init__(self, label: str, calls: list[str], *, blocker: threading.Event | None = None):
        self._label = label
        self._calls = calls
        self._blocker = blocker

    def ensure_model(self) -> None:
        if self._blocker is not None:
            # Simulates a slow cold load so the test can assert startup did not
            # wait for it.
            self._blocker.wait(timeout=5)
        self._calls.append(self._label)


def _patch_factories(monkeypatch, live, final) -> None:
    monkeypatch.setattr(
        "app.services.streaming_models.get_live_service", lambda _s: live, raising=False
    )
    monkeypatch.setattr(
        "app.services.streaming_models.get_final_service", lambda _s: final, raising=False
    )


def _run_lifespan(monkeypatch, *, preload: bool) -> FastAPI:
    from app.core import config as cfg
    from app.main import lifespan

    monkeypatch.setenv("STT_STREAM_PRELOAD_MODELS", "true" if preload else "false")
    # get_settings() memoises into a module global; drop it so the env above is
    # what the lifespan actually reads.
    monkeypatch.setattr(cfg, "_settings", None, raising=False)

    app = FastAPI(lifespan=lifespan)

    @app.get("/probe")
    def _probe() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_both_models_are_loaded_at_startup(monkeypatch) -> None:
    calls: list[str] = []
    _patch_factories(
        monkeypatch, _RecordingService("live", calls), _RecordingService("final", calls)
    )

    app = _run_lifespan(monkeypatch, preload=True)
    with TestClient(app) as client:
        assert client.get("/probe").status_code == 200
        # The preload runs in a daemon thread; give it a bounded moment.
        for _ in range(100):
            if len(calls) == 2:
                break
            threading.Event().wait(0.05)

    assert sorted(calls) == ["final", "live"], (
        "both streaming models must be preloaded at boot; lazy loading recreates "
        "the Bulgu 3-F deadlock where a client timeout cancels the load"
    )


def test_startup_does_not_block_on_a_slow_load(monkeypatch) -> None:
    calls: list[str] = []
    blocker = threading.Event()
    _patch_factories(
        monkeypatch,
        _RecordingService("live", calls, blocker=blocker),
        _RecordingService("final", calls),
    )

    app = _run_lifespan(monkeypatch, preload=True)
    try:
        with TestClient(app) as client:
            # Startup completed and the app serves traffic even though the
            # "model load" is still blocked. If startup awaited the load, health
            # probes would fail for its whole duration and a supervisor would
            # kill the service mid-load — the same never-finishes loop, one
            # layer down.
            assert client.get("/probe").status_code == 200
            assert calls == []
    finally:
        blocker.set()


def test_preload_failure_does_not_take_the_service_down(monkeypatch) -> None:
    class _Exploding:
        def ensure_model(self) -> None:
            raise RuntimeError("model weights unavailable")

    calls: list[str] = []
    _patch_factories(monkeypatch, _Exploding(), _RecordingService("final", calls))

    app = _run_lifespan(monkeypatch, preload=True)
    with TestClient(app) as client:
        assert client.get("/probe").status_code == 200
        for _ in range(100):
            if calls:
                break
            threading.Event().wait(0.05)

    # The live preload raised, yet startup survived and the final model was
    # still attempted: behaviour degrades to the old lazy path rather than
    # taking the whole service with it.
    assert calls == ["final"]


def test_preload_can_be_disabled(monkeypatch) -> None:
    calls: list[str] = []
    _patch_factories(
        monkeypatch, _RecordingService("live", calls), _RecordingService("final", calls)
    )

    app = _run_lifespan(monkeypatch, preload=False)
    with TestClient(app) as client:
        assert client.get("/probe").status_code == 200
        threading.Event().wait(0.2)

    assert calls == [], "STREAM_PRELOAD_MODELS=false must leave loading to the WS path"
