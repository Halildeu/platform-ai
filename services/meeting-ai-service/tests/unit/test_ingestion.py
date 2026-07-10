"""#244 AI-1 — meeting-service aggregate-ingestion client, httpx mocked."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse
from app.services.ingestion import (
    IngestionOutcome,
    ServiceTokenClient,
    submit_analysis_result,
)

TOKEN_URL = "http://keycloak/realms/platform-test/protocol/openid-connect/token"
MEETING_SERVICE_URL = "http://meeting-service:8080"


def _settings(**overrides: object) -> Settings:
    base = {
        "ingestion_enabled": True,
        "meeting_service_base_url": MEETING_SERVICE_URL,
        "meeting_service_token_url": TOKEN_URL,
        "meeting_service_client_id": "meeting-ai-service",
        "meeting_service_client_secret": "s3cr3t",
        "ingestion_timeout_sec": 5,
        "ingestion_max_attempts": 3,
    }
    base.update(overrides)
    return Settings(**base)


def _result(**overrides: object) -> AnalyzeResponse:
    base = {
        "summary": "Toplanti tamamlandi.",
        "summary_grounding_status": "verified",
        "decisions": ["Butce onaylandi."],
        "action_items": [ActionItem(text="Rapor hazirla", owner="Ayse", due_date="cuma")],
        "citations": [],
        "rejected_claims": [],
        "redacted": True,
        "redaction_count": 0,
        "backend": "mock",
        "model": "mock-v1",
        "elapsed_ms": 5,
    }
    base.update(overrides)
    return AnalyzeResponse(**base)


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "fake-token", "expires_in": 300},
        request=httpx.Request("POST", TOKEN_URL),
    )


def _ingestion_response(status: int, **body: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request(
            "POST", f"{MEETING_SERVICE_URL}/internal/v1/meetings/x/analysis-results"
        ),
    )


def _router(token_resp: httpx.Response, ingestion_responses: list[httpx.Response]):
    """Routes httpx.post by URL: token endpoint vs ingestion endpoint (queue)."""
    calls: list[tuple[str, dict]] = []
    remaining = list(ingestion_responses)

    def _post(url: str, **kwargs: object) -> httpx.Response:
        calls.append((url, kwargs))
        if url == TOKEN_URL:
            return token_resp
        return remaining.pop(0)

    return _post, calls


def test_disabled_skips_without_any_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda url, **k: calls.append(url) or _token_response())

    outcome = submit_analysis_result(
        _settings(ingestion_enabled=False),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.SKIPPED_DISABLED
    assert calls == []


def test_missing_meeting_id_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP expected"))
    )

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id=None,
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.SKIPPED_NO_MEETING_ID


def test_missing_session_id_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP expected"))
    )

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id=None,
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.SKIPPED_NO_SESSION_ID


def test_success_sends_bearer_and_idempotency_key_matching_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, calls = _router(
        _token_response(), [_ingestion_response(200, analysis_run_id="whatever", replayed=False)]
    )
    monkeypatch.setattr(httpx, "post", post)

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.SUCCESS
    ingestion_call = calls[-1]
    url, kwargs = ingestion_call
    assert url == f"{MEETING_SERVICE_URL}/internal/v1/meetings/m-1/analysis-results"
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token"
    idempotency_key = kwargs["headers"]["Idempotency-Key"]
    assert kwargs["json"]["analysis_run_id"] == idempotency_key
    assert kwargs["json"]["meetingId"] == "m-1"
    assert kwargs["json"]["transcript_id"] == "SES-1"
    assert kwargs["json"]["summary"] == "Toplanti tamamlandi."
    assert kwargs["json"]["actions"][0]["owner"] == "Ayse"


def test_replayed_response_maps_to_replayed_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    post, _ = _router(
        _token_response(), [_ingestion_response(200, analysis_run_id="x", replayed=True)]
    )
    monkeypatch.setattr(httpx, "post", post)

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.REPLAYED


def test_terminal_conflict_fails_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    post, calls = _router(
        _token_response(), [_ingestion_response(409, error="IDEMPOTENCY_CONFLICT")]
    )
    monkeypatch.setattr(httpx, "post", post)

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.FAILED
    ingestion_calls = [c for c in calls if c[0] != TOKEN_URL]
    assert len(ingestion_calls) == 1  # no retry on a terminal 4xx


def test_retryable_503_then_success_reuses_same_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, calls = _router(
        _token_response(),
        [_ingestion_response(503), _ingestion_response(200, analysis_run_id="x", replayed=False)],
    )
    monkeypatch.setattr(httpx, "post", post)

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.SUCCESS
    ingestion_calls = [c for c in calls if c[0] != TOKEN_URL]
    assert len(ingestion_calls) == 2
    key_attempt_1 = ingestion_calls[0][1]["headers"]["Idempotency-Key"]
    key_attempt_2 = ingestion_calls[1][1]["headers"]["Idempotency-Key"]
    assert key_attempt_1 == key_attempt_2  # #244 AI-1: retry-safe = same analysisRunId


def test_exhausted_retries_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    post, calls = _router(
        _token_response(),
        [_ingestion_response(503), _ingestion_response(503), _ingestion_response(503)],
    )
    monkeypatch.setattr(httpx, "post", post)

    outcome = submit_analysis_result(
        _settings(ingestion_max_attempts=3),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.FAILED
    ingestion_calls = [c for c in calls if c[0] != TOKEN_URL]
    assert len(ingestion_calls) == 3


def test_network_error_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _post(url: str, **kwargs: object) -> httpx.Response:
        if url == TOKEN_URL:
            return _token_response()
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _post)

    outcome = submit_analysis_result(
        _settings(ingestion_max_attempts=2),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.FAILED


def test_401_invalidates_cached_token_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    token_calls = {"n": 0}

    def _post(url: str, **kwargs: object) -> httpx.Response:
        if url == TOKEN_URL:
            token_calls["n"] += 1
            return _token_response()
        if token_calls["n"] == 1:
            return _ingestion_response(401)
        return _ingestion_response(200, analysis_run_id="x", replayed=False)

    monkeypatch.setattr(httpx, "post", _post)

    outcome = submit_analysis_result(
        _settings(),
        ServiceTokenClient(_settings()),
        meeting_id="m-1",
        session_id="SES-1",
        transcript="Merhaba.",
        result=_result(),
    )

    assert outcome is IngestionOutcome.SUCCESS
    assert token_calls["n"] == 2  # first token, invalidated on 401, refetched


def test_token_client_caches_until_near_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), _token_response())[1],
    )

    client = ServiceTokenClient(_settings())
    first = client.get_token()
    second = client.get_token()

    assert first == second == "fake-token"
    assert calls["n"] == 1  # second call served from cache
