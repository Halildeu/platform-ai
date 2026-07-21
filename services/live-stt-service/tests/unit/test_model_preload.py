"""Startup preload of the streaming models (Faz 24 Bulgu 3-F).

Loading the models lazily on the first WebSocket deadlocked against client
patience: a cold load takes minutes, the desktop recorder waits 10s for
`ready`, and its disconnect cancelled the load ("WS disconnected during model
load"). Every attempt restarted from zero, so no session could ever start.

These tests pin the properties that keep that deadlock impossible: both models
load at boot, startup remains live, readiness fails closed, retries are bounded,
and shutdown cannot start another model worker after cancellation.
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


def _run_lifespan(monkeypatch, *, preload: bool, max_attempts: int = 1) -> FastAPI:
    from app.api import health
    from app.core import config as cfg
    from app.main import lifespan

    monkeypatch.setenv("STT_STREAM_PRELOAD_MODELS", "true" if preload else "false")
    monkeypatch.setenv("STT_STREAM_PRELOAD_MAX_ATTEMPTS", str(max_attempts))
    monkeypatch.setenv("STT_STREAM_PRELOAD_RETRY_BASE_SEC", "0.1")
    # get_settings() memoises into a module global; drop it so the env above is
    # what the lifespan actually reads.
    monkeypatch.setattr(cfg, "_settings", None, raising=False)

    app = FastAPI(lifespan=lifespan)
    app.include_router(health.router)

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
            readiness = client.get("/ready")
            assert readiness.status_code == 503
            assert readiness.json()["status"] == "loading"
            assert calls == []
    finally:
        blocker.set()


def test_preload_failure_keeps_liveness_but_fails_readiness(monkeypatch) -> None:
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
        readiness = client.get("/ready")
        assert readiness.status_code == 503
        assert readiness.json()["status"] == "failed"

    # The live preload raised, yet liveness survived and the final model was
    # still attempted. /ready remains fail-closed so traffic is not sent to a
    # process that cannot start the customer recording stream.
    assert calls == ["final"]


def test_preload_retries_are_bounded_and_can_recover(monkeypatch) -> None:
    class _FailsOnce:
        attempts = 0

        def ensure_model(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient load failure")

    calls: list[str] = []
    live = _FailsOnce()
    _patch_factories(monkeypatch, live, _RecordingService("final", calls))

    app = _run_lifespan(monkeypatch, preload=True, max_attempts=2)
    with TestClient(app) as client:
        for _ in range(100):
            if client.get("/ready").status_code == 200:
                break
            threading.Event().wait(0.02)
        readiness = client.get("/ready")
        assert readiness.status_code == 200
        assert readiness.json()["attempts"] == {"live": 2, "final": 1}

    assert live.attempts == 2
    assert calls == ["final"]


def test_preload_can_be_disabled(monkeypatch) -> None:
    calls: list[str] = []
    _patch_factories(
        monkeypatch, _RecordingService("live", calls), _RecordingService("final", calls)
    )

    app = _run_lifespan(monkeypatch, preload=False)
    with TestClient(app) as client:
        assert client.get("/probe").status_code == 200
        assert client.get("/ready").status_code == 503
        threading.Event().wait(0.2)

    assert calls == [], "STREAM_PRELOAD_MODELS=false must leave loading to the WS path"


def test_preload_log_names_the_model_and_duration(monkeypatch, caplog) -> None:
    """The rendered message must identify which model loaded, and how long.

    The service log format renders only `%(message)s`, so anything passed via
    `extra` is invisible to an operator tailing the log. With both models
    emitting an identical line there was no way to tell whether one or both
    had finished — during the Bulgu 3-F rollout that ambiguity cost a
    diagnostic step.
    """
    import logging

    calls: list[str] = []
    _patch_factories(
        monkeypatch, _RecordingService("live", calls), _RecordingService("final", calls)
    )

    app = _run_lifespan(monkeypatch, preload=True)
    with caplog.at_level(logging.INFO, logger="app.main"), TestClient(app) as client:
        assert client.get("/probe").status_code == 200
        for _ in range(100):
            if len(calls) == 2:
                break
            threading.Event().wait(0.05)
        threading.Event().wait(0.2)

    rendered = [r.getMessage() for r in caplog.records if "preloaded" in r.getMessage()]
    assert len(rendered) == 2, f"expected one line per model, got {rendered}"
    assert any("role=live" in m for m in rendered), rendered
    assert any("role=final" in m for m in rendered), rendered
    assert all("elapsed_sec=" in m for m in rendered), rendered


def test_shutdown_cancels_before_starting_the_next_model(monkeypatch) -> None:
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class _BlockingLive:
        def ensure_model(self) -> None:
            entered.set()
            release.wait(timeout=5)
            calls.append("live")

    _patch_factories(monkeypatch, _BlockingLive(), _RecordingService("final", calls))
    app = _run_lifespan(monkeypatch, preload=True)
    client = TestClient(app)
    client.__enter__()
    assert entered.wait(timeout=1)

    shutdown = threading.Thread(target=lambda: client.__exit__(None, None, None))
    shutdown.start()
    for _ in range(100):
        if app.state.streaming_preload.stop_event.is_set():
            break
        threading.Event().wait(0.01)
    release.set()
    shutdown.join(timeout=2)

    assert not shutdown.is_alive()
    assert calls == ["live"]
