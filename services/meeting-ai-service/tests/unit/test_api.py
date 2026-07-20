"""API smoke tests via FastAPI TestClient."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_analyze_mock_returns_summary() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={"transcript": "Toplantı başladı. Bütçe kararlaştırıldı. Ali hazırlayacak."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["redacted"] is True
    assert body["summary_grounding_status"] in ("verified", "partial_verified")
    assert body["summary_citations"]
    assert len(body["summary"]) > 0


def test_analyze_redacts_pii_before_response() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={"transcript": "Ali ali@example.com adresinden gönderecek. Karar verildi."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["redaction_count"] >= 1
    blob = body["summary"] + " ".join(a["text"] for a in body["action_items"])
    assert "ali@example.com" not in blob


def test_analyze_empty_transcript_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": ""})
    assert resp.status_code == 422


def test_analyze_too_large_413(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MAI_MAX_TRANSCRIPT_CHARS", "10")
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "x" * 50})
    assert resp.status_code == 413


def test_analyze_llm_backend_501(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MAI_BACKEND", "anthropic")
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "Bir metin."})
    assert resp.status_code == 501


def test_analyze_ollama_down_502(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("connection refused")

    monkeypatch.setenv("MAI_BACKEND", "ollama")
    monkeypatch.setattr(httpx, "post", _boom)
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "Bir metin."})
    assert resp.status_code == 502


def test_analyze_segments_attach_timestamps(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # #162: STT segments in the request → citations carry wall-clock start_sec
    monkeypatch.setenv("MAI_REDACT_PII", "False")  # mock backend: keep text verbatim
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={
                "transcript": "Bütçe artışı onaylandı. Ali raporu hazırlayacak.",
                "segments": [
                    {"text": "Bütçe artışı onaylandı.", "start": 0.0, "end": 3.0},
                    {"text": "Ali raporu hazırlayacak.", "start": 3.0, "end": 6.0},
                ],
            },
        )
    assert resp.status_code == 200
    citations = resp.json()["citations"]
    grounded = [c for c in citations if c["grounded"]]
    assert grounded, "expected at least one grounded citation"
    assert all(c["start_sec"] is not None for c in grounded)


def test_analyze_without_segments_has_no_timestamps() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={"transcript": "Bütçe artışı onaylandı. Ali raporu hazırlayacak."},
        )
    assert resp.status_code == 200
    assert all(c["start_sec"] is None for c in resp.json()["citations"])


def test_health_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["analysis_delivery"]["status"] == "disabled"


def test_metrics_endpoint() -> None:
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "mai_analyze_total" in resp.text


def test_analyze_nonmock_residual_pii_blocked_422(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # ADR-0043 D3 fail-closed: a non-mock backend + PII that survives precise redaction
    # (a 0-leading 11-digit, missed by the TC/phone patterns) → 422; the LLM is NEVER
    # called (the residual gate runs before the analyzer). No network dependency.
    monkeypatch.setenv("MAI_BACKEND", "ollama")
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "Kayıt 01234567890 girildi."})
    assert resp.status_code == 422


def _configure_ingestion(monkeypatch, tmp_path: Path, *, max_rows: int = 10) -> None:  # type: ignore[no-untyped-def]
    keyring = json.dumps({"v1": base64.b64encode(b"K" * 32).decode()})
    monkeypatch.setenv("MAI_INGESTION_ENABLED", "true")
    monkeypatch.setenv("MAI_MEETING_SERVICE_BASE_URL", "https://127.0.0.1:9")
    monkeypatch.setenv("MAI_MEETING_SERVICE_TOKEN_URL", "https://127.0.0.1:9/token")
    monkeypatch.setenv("MAI_MEETING_SERVICE_CLIENT_ID", "meeting-ai")
    monkeypatch.setenv("MAI_MEETING_SERVICE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MAI_INGESTION_STORE_PATH", str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("MAI_INGESTION_ACTIVE_KEY_ID", "v1")
    monkeypatch.setenv("MAI_INGESTION_ENCRYPTION_KEYS_JSON", keyring)
    monkeypatch.setenv("MAI_INGESTION_TIMEOUT_SEC", "0.1")
    monkeypatch.setenv("MAI_INGESTION_LEASE_SEC", "1")
    monkeypatch.setenv("MAI_INGESTION_SHUTDOWN_GRACE_SEC", "0.1")
    monkeypatch.setenv("MAI_INGESTION_MAX_ROWS", str(max_rows))


def test_analyze_durably_enqueues_before_returning(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _configure_ingestion(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={
                "transcript": "Bütçe kararlaştırıldı.",
                "meeting_id": "11111111-1111-4111-8111-111111111111",
                "session_id": "session-1",
            },
        )
    assert resp.status_code == 200
    assert resp.headers["X-Analysis-Delivery"] == "queued"
    assert len(resp.headers["X-Analysis-Run-Id"]) == 36


def test_analyze_requires_canonical_ids_when_delivery_enabled(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _configure_ingestion(monkeypatch, tmp_path)
    with TestClient(app) as client:
        missing = client.post("/analyze", json={"transcript": "Bir metin."})
        invalid = client.post(
            "/analyze",
            json={"transcript": "Bir metin.", "meeting_id": "not-a-uuid", "session_id": "s"},
        )
    assert missing.status_code == 422
    assert invalid.status_code == 422


def test_analyze_fails_closed_when_durable_queue_is_full(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _configure_ingestion(monkeypatch, tmp_path, max_rows=1)
    payload = {
        "transcript": "Bütçe kararlaştırıldı.",
        "meeting_id": "11111111-1111-4111-8111-111111111111",
        "session_id": "session-1",
    }
    with TestClient(app) as client:
        first = client.post("/analyze", json=payload)
        second = client.post("/analyze", json=payload)
    assert first.status_code == 200
    assert second.status_code == 503
    assert second.headers["Retry-After"] == "30"


# ── Faz 24 live analysis (Zeynep 2026-07-20 kapsam kararı) ─────────────────


def test_analyze_live_marks_is_partial_true_and_threads_version() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze/live",
            json={
                "transcript": "Toplantı başladı. Bütçe kararlaştırıldı.",
                "segment_seq": 42,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    # Live-only metadata differentiators — a live and a final call over the
    # same content produce byte-identical payloads apart from these two fields
    # plus the X-Analysis-* response headers below.
    assert body["is_partial"] is True
    assert body["version"] == 42
    # Response headers pin the same signal for downstream consumers that read
    # metadata before parsing the body (SSE relay, meeting-service ingestion).
    assert resp.headers["X-Analysis-Is-Partial"] == "true"
    assert resp.headers["X-Analysis-Version"] == "42"


def test_analyze_live_defaults_version_zero_when_segment_seq_missing() -> None:
    # A caller (or an early recorder revision) may omit segment_seq. The
    # endpoint stays available; version defaults to 0 so the ingestion side
    # can still order by (meeting_id, version) without null handling.
    with TestClient(app) as client:
        resp = client.post(
            "/analyze/live",
            json={"transcript": "Kısa transcript."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_partial"] is True
    assert body["version"] == 0
    assert resp.headers["X-Analysis-Version"] == "0"


def test_analyze_default_endpoint_stays_final_not_partial() -> None:
    # Regression: the existing /analyze endpoint MUST NOT flip to partial just
    # because segment_seq shows up in the request. `is_partial` defaults False,
    # `version` defaults 0, and `/analyze` never mutates them. This keeps
    # downstream consumers that read `is_partial` from mis-classifying the
    # final analysis result as a live/superseded delivery.
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={
                "transcript": "Toplantı başladı. Bütçe kararlaştırıldı.",
                "segment_seq": 99,  # ignored by /analyze
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_partial"] is False
    assert body["version"] == 0
    # /analyze does not emit the live headers
    assert "X-Analysis-Is-Partial" not in resp.headers
    assert "X-Analysis-Version" not in resp.headers


def test_analyze_live_empty_transcript_422() -> None:
    # Same validation as /analyze — empty transcript is a client contract
    # violation regardless of live/final variant.
    with TestClient(app) as client:
        resp = client.post("/analyze/live", json={"transcript": ""})
    assert resp.status_code == 422


def test_analyze_live_too_large_413(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Transcript size limit is a shared invariant with /analyze; live must
    # NOT sneak an oversized payload past the cap by using the live path.
    monkeypatch.setenv("MAI_MAX_TRANSCRIPT_CHARS", "10")
    with TestClient(app) as client:
        resp = client.post(
            "/analyze/live",
            json={"transcript": "x" * 50, "segment_seq": 1},
        )
    assert resp.status_code == 413


def test_analyze_live_redacts_pii_same_as_analyze() -> None:
    # KVKK invariant: the redaction guard runs on live just like final.
    # A live call MUST NOT leak PII in the response payload because "it's
    # only partial" — the delivery path may still fan out to viewers.
    with TestClient(app) as client:
        resp = client.post(
            "/analyze/live",
            json={
                "transcript": "Ali ali@example.com adresinden gönderecek. Karar verildi.",
                "segment_seq": 5,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["redaction_count"] >= 1
    blob = body["summary"] + " ".join(a["text"] for a in body["action_items"])
    assert "ali@example.com" not in blob


# ── Faz 24 live-stream SSE relay (İ2) ──────────────────────────────────────
#
# Behavioural coverage split:
#   - LiveStreamHub itself (pub/sub, drop-oldest, isolation) is exercised in
#     tests/unit/test_live_stream_hub.py against the class directly.
#   - Here we prove the WIRING: /analyze/live publishes into whatever hub is
#     mounted on `app.state.live_stream_hub`, using a stub that captures
#     calls. This avoids driving a real SSE stream through TestClient (which
#     deadlocks on a keep-alive endpoint because sync `iter_bytes` does not
#     honour a read timeout the way an async client would).
#   - End-to-end SSE-over-HTTP is validated by Faz D acceptance smoke
#     (browser MCP + curl chain) rather than a TestClient integration test —
#     the sync client cannot cleanly interrupt an SSE stream mid-flight.


def test_analyze_live_publishes_result_to_hub_on_state() -> None:
    """`/analyze/live` must call `app.state.live_stream_hub.publish` for a
    request that carries a `meeting_id`, and pass the response payload.

    A no-op stub replaces the real hub for the duration of the test so we
    exercise the wiring without booting the SSE reader.
    """

    captured: list[tuple[str, dict[str, object]]] = []

    class _StubHub:
        async def publish(self, meeting_id: str, event: dict[str, object]) -> tuple[int, int]:
            captured.append((meeting_id, event))
            return (0, 0)

    meeting_id = "66666666-6666-4666-8666-666666666666"
    with TestClient(app) as client:
        # Swap the real hub for a stub AFTER the lifespan install so we do
        # not race the app startup.
        app.state.live_stream_hub = _StubHub()
        try:
            resp = client.post(
                "/analyze/live",
                json={
                    "transcript": "Toplantı başladı. Bütçe kararlaştırıldı.",
                    "meeting_id": meeting_id,
                    "segment_seq": 11,
                },
            )
        finally:
            # Do not leak the stub to sibling tests in this session.
            app.state.live_stream_hub = None

    assert resp.status_code == 200
    assert len(captured) == 1
    got_meeting, got_event = captured[0]
    assert got_meeting == meeting_id
    # The payload MUST be the AnalyzeResponse dict (not the request) — check
    # a few pinned fields that only the response carries.
    assert got_event.get("is_partial") is True
    assert got_event.get("version") == 11
    assert "summary" in got_event
    assert "decisions" in got_event


def test_analyze_live_without_meeting_id_does_not_publish() -> None:
    """No `meeting_id` on the request → no `publish` call.

    A live analysis with no meeting_id has no channel to fan out to, so the
    endpoint MUST NOT invoke publish with an empty string (which would leak
    events across meetings via the shared bucket).
    """

    captured: list[str] = []

    class _StubHub:
        async def publish(self, meeting_id: str, event: dict[str, object]) -> tuple[int, int]:
            captured.append(meeting_id)
            return (0, 0)

    with TestClient(app) as client:
        app.state.live_stream_hub = _StubHub()
        try:
            resp = client.post(
                "/analyze/live",
                json={
                    "transcript": "Toplantı başladı.",
                    "segment_seq": 3,
                    # no meeting_id
                },
            )
        finally:
            app.state.live_stream_hub = None

    assert resp.status_code == 200
    assert captured == [], "publish called with a meeting-id-less request"


def test_analyze_live_publish_error_does_not_break_the_request() -> None:
    """If the hub raises during publish, `/analyze/live` still returns 200.

    Live relay is best-effort — a broken relay MUST NOT surface as a 5xx to
    the analyzer caller (audio-gateway / recorder). We rely on the drop
    metric to alert instead.
    """

    class _BrokenHub:
        async def publish(self, meeting_id: str, event: dict[str, object]) -> tuple[int, int]:
            raise RuntimeError("simulated hub failure")

    with TestClient(app) as client:
        app.state.live_stream_hub = _BrokenHub()
        try:
            resp = client.post(
                "/analyze/live",
                json={
                    "transcript": "Toplantı başladı.",
                    "meeting_id": "77777777-7777-4777-8777-777777777777",
                    "segment_seq": 1,
                },
            )
        finally:
            app.state.live_stream_hub = None

    assert resp.status_code == 200, resp.text


def test_live_stream_endpoint_503_when_hub_missing() -> None:
    """If the SSE endpoint is hit before the hub is installed (or after it is
    torn down for a test), it must fail-fast with 503 rather than serving an
    empty stream forever.
    """

    with TestClient(app) as client:
        original = getattr(app.state, "live_stream_hub", None)
        app.state.live_stream_hub = None
        try:
            resp = client.get(
                "/analyze/live/stream/88888888-8888-4888-8888-888888888888",
                timeout=2.0,
            )
        finally:
            app.state.live_stream_hub = original

    assert resp.status_code == 503
