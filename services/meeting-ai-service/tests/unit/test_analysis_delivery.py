"""Contract mapping and worker lifecycle tests for durable analysis delivery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse, Citation, RejectedClaim
from app.services.analysis_delivery import (
    AnalysisDeliveryContractError,
    AnalysisDeliveryRuntime,
    build_ingestion_payload,
)
from app.services.durable_outbox import ClaimedMessage, PayloadCipher, SqliteOutboxStore
from app.services.meeting_service_client import DeliveryAttempt, DeliveryDisposition

KEY = b"K" * 32
MEETING_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
FINALIZED_AT = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, outcomes: list[DeliveryAttempt]) -> None:
        self._outcomes = deque(outcomes)
        self.calls = []

    async def deliver(self, message):  # type: ignore[no-untyped-def]
        self.calls.append(message)
        return self._outcomes.popleft()


class BlockingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def deliver(self, message):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()
        return DeliveryAttempt(DeliveryDisposition.DELIVERED)


def _settings(path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "ingestion_enabled": True,
        "meeting_service_base_url": "https://meeting.invalid",
        "meeting_service_token_url": "https://auth.invalid/token",
        "meeting_service_client_id": "meeting-ai",
        "meeting_service_client_secret": SecretStr("secret"),
        "ingestion_store_path": path,
        "ingestion_active_key_id": "v1",
        "ingestion_encryption_keys_json": SecretStr(
            json.dumps({"v1": base64.b64encode(KEY).decode()})
        ),
        "ingestion_timeout_sec": 0.1,
        "ingestion_lease_sec": 1.0,
        "ingestion_poll_interval_sec": 0.05,
        "ingestion_base_backoff_sec": 0.1,
        "ingestion_max_backoff_sec": 1.0,
        "ingestion_jitter_ratio": 0.0,
        "ingestion_shutdown_grace_sec": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


def _result() -> AnalyzeResponse:
    citation = Citation(
        claim="Bütçe onaylandı.",
        source_index=0,
        source_text="Bütçe onaylandı.",
        similarity=1.0,
        grounded=True,
        status="PASSED",
        source_char_start=0,
        source_char_end=17,
        source_hash="a" * 64,
        quote_hash="b" * 64,
    )
    return AnalyzeResponse(
        summary="Bütçe onaylandı.",
        summary_grounding_status="verified",
        summary_citations=[citation],
        decisions=["Bütçe onaylandı."],
        action_items=[ActionItem(text="Raporu hazırla", owner="Ayşe", due_date="cuma")],
        citations=[citation],
        rejected_claims=[
            RejectedClaim(
                claim="Desteksiz",
                kind="summary",
                status="FAILED",
                reason="ungrounded",
                similarity=0.1,
            )
        ],
        ungrounded_count=1,
        redacted=True,
        redaction_count=2,
        backend="ollama",
        model="qwen",
        elapsed_ms=12,
    )


def _canonical_tuple() -> dict[str, object]:
    return {
        "tenant_id": TENANT_ID,
        "finalization_version": 1,
        "finalized_at": FINALIZED_AT,
        "analysis_spec_version": "meeting-intelligence-v1",
    }


def test_payload_matches_backend_contract_and_preserves_grounding(tmp_path: Path) -> None:
    payload = build_ingestion_payload(
        settings=_settings(tmp_path / "outbox.sqlite3"),
        meeting_id=MEETING_ID,
        tenant_id=TENANT_ID,
        session_id="session-1",
        finalization_version=4,
        finalized_at=datetime(2026, 7, 11, 19, 55, tzinfo=UTC),
        analysis_spec_version="meeting-intelligence-v1",
        transcript="Bütçe onaylandı.",
        result=_result(),
        generated_at=datetime(2026, 7, 11, 20, 0, tzinfo=UTC),
    )
    assert payload["meeting_id"] == MEETING_ID
    assert payload["transcript_session_id"] == "session-1"
    assert payload["finalization_version"] == 4
    assert payload["finalized_at"] == "2026-07-11T19:55:00Z"
    assert payload["analysis_spec_version"] == "meeting-intelligence-v1"
    assert payload["_canonical_tenant_id"] == TENANT_ID
    assert len(str(payload["transcript_sha256"])) == 64
    assert payload["summary_grounding_status"] == "verified"
    assert payload["summary_citations"]
    assert payload["citations"]
    assert payload["rejected_claims"]
    assert payload["actions"] == [{"text": "Raporu hazırla", "assignee": "Ayşe", "due": None}]
    assert payload["generated_at"] == "2026-07-11T20:00:00Z"
    assert set(payload) == {
        "meeting_id",
        "transcript_session_id",
        "transcript_sha256",
        "finalization_version",
        "finalized_at",
        "analysis_spec_version",
        "analyzer_contract_version",
        "model",
        "backend",
        "prompt_version",
        "summary",
        "summary_grounding_status",
        "summary_citations",
        "citations",
        "rejected_claims",
        "ungrounded_count",
        "redacted",
        "redaction_count",
        "generated_at",
        "decisions",
        "actions",
        "supersedes_analysis_run_id",
        "_canonical_tenant_id",
    }
    assert "transcript" not in payload


def test_valid_iso_due_is_normalized_to_utc(tmp_path: Path) -> None:
    result = _result()
    result.action_items[0].due_date = "2026-07-12T12:00:00+03:00"
    payload = build_ingestion_payload(
        settings=_settings(tmp_path / "outbox.sqlite3"),
        meeting_id=MEETING_ID,
        **_canonical_tuple(),
        session_id="session-1",
        transcript="A",
        result=result,
        generated_at=datetime.now(UTC),
    )
    assert payload["actions"] == [
        {"text": "Raporu hazırla", "assignee": "Ayşe", "due": "2026-07-12T09:00:00Z"}
    ]


def test_delivery_refuses_unredacted_or_backend_oversized_output(tmp_path: Path) -> None:
    result = _result()
    result.redacted = False
    with pytest.raises(AnalysisDeliveryContractError):
        build_ingestion_payload(
            settings=_settings(tmp_path / "outbox.sqlite3"),
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="A",
            result=result,
            generated_at=datetime.now(UTC),
        )

    result = _result()
    result.decisions = ["x" * 4001]
    with pytest.raises(AnalysisDeliveryContractError):
        build_ingestion_payload(
            settings=_settings(tmp_path / "outbox.sqlite3"),
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="A",
            result=result,
            generated_at=datetime.now(UTC),
        )


def test_enqueue_survives_restart_and_is_delivered(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(path)
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        first = AnalysisDeliveryRuntime(
            settings,
            store=store,
            transport=FakeTransport([]),
        )
        run_id = await first.enqueue_analysis(
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="Bütçe onaylandı.",
            result=_result(),
        )
        assert run_id is not None
        assert store.summary().pending == 1

        transport = FakeTransport([DeliveryAttempt(DeliveryDisposition.DELIVERED)])
        restarted = AnalysisDeliveryRuntime(settings, store=store, transport=transport)
        await restarted.start()
        for _ in range(50):
            if store.summary().pending == 0 and store.summary().in_flight == 0:
                break
            await asyncio.sleep(0.02)
        await restarted.stop()

        assert len(transport.calls) == 1
        assert transport.calls[0].analysis_run_id == run_id
        assert store.summary().pending == 0

    asyncio.run(scenario())


def test_noncanonical_analysis_is_rejected_before_outbox_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        runtime = AnalysisDeliveryRuntime(
            _settings(path),
            store=store,
            transport=FakeTransport([]),
        )

        with pytest.raises(
            AnalysisDeliveryContractError,
            match="canonical transcript.ready tuple",
        ):
            await runtime.enqueue_analysis(
                meeting_id=MEETING_ID,
                session_id=SESSION_ID,
                transcript="A",
                result=_result(),
            )

        summary = store.summary()
        assert summary.pending == summary.in_flight == summary.dead == 0

    asyncio.run(scenario())


def test_lost_201_restart_uses_fresh_capability_for_single_run_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, list[str], list[str], bytes]:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(
            path,
            meeting_service_base_url="https://meeting.test",
            meeting_service_token_url="https://auth.test/token",
            transcript_service_base_url="https://transcript.test",
            transcript_service_snapshot_path_template=(
                "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}"
                "/sessions/{session_id}/finalizations/{finalization_version}"
            ),
            transcript_service_token_url="https://auth.test/token",
            transcript_service_client_id="meeting-ai-ready",
            transcript_service_client_secret=SecretStr("transcript-secret"),
        )
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        transcript = "Bütçe onaylandı."
        transcript_sha = hashlib.sha256(transcript.encode()).hexdigest()
        finalized_at = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)
        run_id = "44444444-4444-4444-8444-444444444444"
        payload = build_ingestion_payload(
            settings=settings,
            meeting_id=MEETING_ID,
            tenant_id=TENANT_ID,
            session_id=SESSION_ID,
            finalization_version=1,
            finalized_at=finalized_at,
            analysis_spec_version=settings.analysis_spec_version,
            transcript=transcript,
            result=_result(),
            generated_at=datetime(2026, 7, 18, 1, 1, tzinfo=UTC),
        )
        store.enqueue(analysis_run_id=run_id, meeting_id=MEETING_ID, payload=payload)

        capabilities: list[str] = []
        post_outcomes: list[str] = []
        persisted_runs: dict[str, dict[str, object]] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "service-token"})
            if request.url.host == "transcript.test":
                capability = f"one-use-capability-{len(capabilities) + 1}"
                capabilities.append(capability)
                assert request.headers["X-Analysis-Run-Id"] == run_id
                assert request.headers["X-Analysis-Spec-Version"] == settings.analysis_spec_version
                return httpx.Response(
                    200,
                    json={
                        "tenantId": TENANT_ID,
                        "meetingId": MEETING_ID,
                        "sessionId": SESSION_ID,
                        "finalizationVersion": 1,
                        "finalizedAt": "2026-07-18T01:00:00Z",
                        "state": "FINALIZED",
                        "transcript": transcript,
                        "transcriptSha256": transcript_sha,
                        "segmentCount": 1,
                        "segments": [{"text": transcript, "start": 0.0, "end": 1.0}],
                    },
                    headers={
                        "X-Analysis-Job-Capability": capability,
                        "X-Analysis-Job-Capability-Expires-At": "2099-07-18T01:15:00Z",
                    },
                )

            body: dict[str, object] = json.loads(request.content)
            capability = request.headers["X-Analysis-Job-Capability"]
            assert capability == capabilities[-1]
            assert capability not in capabilities[:-1]
            assert request.headers["Idempotency-Key"] == run_id
            assert body["finalization_version"] == 1
            assert body["finalized_at"] == "2026-07-18T01:00:00Z"
            assert body["analysis_spec_version"] == settings.analysis_spec_version
            assert "_canonical_tenant_id" not in body
            existing = persisted_runs.get(run_id)
            if existing is None:
                persisted_runs[run_id] = body
                post_outcomes.append("201-response-lost")
                raise httpx.ReadError("response lost after commit", request=request)
            assert existing == body
            post_outcomes.append("200-replay")
            return httpx.Response(200, json={"idempotent_replay": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = AnalysisDeliveryRuntime(settings, store=store, http_client=client)
            first_message = store.claim_next(owner=first._owner, lease_sec=1.0)
            assert first_message is not None
            await first._deliver(first_message)
            await first.stop()

            await asyncio.sleep(0.11)
            restarted = AnalysisDeliveryRuntime(settings, store=store, http_client=client)
            replay_message = store.claim_next(owner=restarted._owner, lease_sec=1.0)
            assert replay_message is not None
            await restarted._deliver(replay_message)
            await restarted.stop()

        summary = store.summary()
        assert summary.pending == summary.in_flight == summary.dead == 0
        durable_bytes = path.read_bytes()
        return len(persisted_runs), capabilities, post_outcomes, durable_bytes

    run_count, capabilities, post_outcomes, durable_bytes = asyncio.run(scenario())
    assert run_count == 1
    assert capabilities == ["one-use-capability-1", "one-use-capability-2"]
    assert post_outcomes == ["201-response-lost", "200-replay"]
    assert b"one-use-capability" not in durable_bytes
    assert "Bütçe onaylandı.".encode() not in durable_bytes


def test_retry_then_success_reuses_same_analysis_run_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(path)
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        transport = FakeTransport(
            [
                DeliveryAttempt(DeliveryDisposition.RETRY, "http_503"),
                DeliveryAttempt(DeliveryDisposition.REPLAYED),
            ]
        )
        runtime = AnalysisDeliveryRuntime(settings, store=store, transport=transport)
        run_id = await runtime.enqueue_analysis(
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="A",
            result=_result(),
        )
        await runtime.start()
        for _ in range(100):
            if len(transport.calls) == 2:
                break
            await asyncio.sleep(0.02)
        await runtime.stop()
        assert [call.analysis_run_id for call in transport.calls] == [run_id, run_id]
        assert store.summary().pending == 0

    asyncio.run(scenario())


def test_enqueue_does_not_wait_for_blocked_network_delivery(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(path)
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        transport = BlockingTransport()
        runtime = AnalysisDeliveryRuntime(settings, store=store, transport=transport)
        await runtime.start()

        run_id = await asyncio.wait_for(
            runtime.enqueue_analysis(
                meeting_id=MEETING_ID,
                **_canonical_tuple(),
                session_id="session-1",
                transcript="A",
                result=_result(),
            ),
            timeout=0.5,
        )
        await asyncio.wait_for(transport.started.wait(), timeout=0.5)
        assert run_id is not None
        assert store.summary().in_flight == 1

        transport.release.set()
        await runtime.stop()
        assert store.summary().in_flight == 0

    asyncio.run(scenario())


def test_shutdown_cancellation_preserves_leased_payload_for_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(path, ingestion_shutdown_grace_sec=0.1)
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        transport = BlockingTransport()
        runtime = AnalysisDeliveryRuntime(settings, store=store, transport=transport)
        await runtime.enqueue_analysis(
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="A",
            result=_result(),
        )
        await runtime.start()
        await asyncio.wait_for(transport.started.wait(), timeout=0.5)
        await runtime.stop()

        assert store.summary().in_flight == 1
        recovered = store.claim_next(
            owner="restarted-worker",
            lease_sec=1.0,
            now=time.time() + 2.0,
        )
        assert recovered is not None
        assert recovered.attempt_count == 2
        assert store.mark_delivered(
            analysis_run_id=recovered.analysis_run_id,
            owner="restarted-worker",
        )

    asyncio.run(scenario())


def test_retry_limit_moves_payload_to_dead_letter(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(path, ingestion_max_attempts=2)
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        transport = FakeTransport(
            [
                DeliveryAttempt(DeliveryDisposition.RETRY, "ingestion_http_503"),
                DeliveryAttempt(DeliveryDisposition.RETRY, "ingestion_http_503"),
            ]
        )
        runtime = AnalysisDeliveryRuntime(settings, store=store, transport=transport)
        await runtime.enqueue_analysis(
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="A",
            result=_result(),
        )
        await runtime.start()
        for _ in range(100):
            if store.summary().dead == 1:
                break
            await asyncio.sleep(0.02)
        await runtime.stop()

        assert len(transport.calls) == 2
        assert store.summary().dead == 1

    asyncio.run(scenario())


def test_retry_after_and_jitter_cannot_exceed_configured_backoff_cap(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "outbox.sqlite3",
        ingestion_base_backoff_sec=1.0,
        ingestion_max_backoff_sec=4.0,
        ingestion_jitter_ratio=0.5,
    )
    runtime = AnalysisDeliveryRuntime(settings, transport=FakeTransport([]))
    message = ClaimedMessage(
        analysis_run_id="22222222-2222-4222-8222-222222222222",
        meeting_id=MEETING_ID,
        payload={},
        attempt_count=99,
        created_at=1.0,
    )

    assert runtime._retry_delay(
        message,
        DeliveryAttempt(DeliveryDisposition.RETRY, retry_after_sec=999_999_999.0),
    ) == pytest.approx(4.0)


def test_terminal_failure_enters_dead_letter_and_degrades_health(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "outbox.sqlite3"
        settings = _settings(path)
        store = SqliteOutboxStore(path, PayloadCipher({"v1": KEY}, "v1"), max_rows=10)
        runtime = AnalysisDeliveryRuntime(
            settings,
            store=store,
            transport=FakeTransport(
                [DeliveryAttempt(DeliveryDisposition.TERMINAL, "ingestion_http_400")]
            ),
        )
        await runtime.enqueue_analysis(
            meeting_id=MEETING_ID,
            **_canonical_tuple(),
            session_id="session-1",
            transcript="A",
            result=_result(),
        )
        await runtime.start()
        for _ in range(50):
            if store.summary().dead == 1:
                break
            await asyncio.sleep(0.02)
        health = await runtime.health()
        await runtime.stop()
        assert health.status == "degraded"
        assert health.dead_letter == 1

    asyncio.run(scenario())
