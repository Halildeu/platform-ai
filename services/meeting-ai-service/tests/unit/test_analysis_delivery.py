"""Contract mapping and worker lifecycle tests for durable analysis delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse, Citation, RejectedClaim
from app.services.analysis_delivery import (
    AnalysisDeliveryContractError,
    AnalysisDeliveryRuntime,
    build_ingestion_payload,
)
from app.services.durable_outbox import PayloadCipher, SqliteOutboxStore
from app.services.meeting_service_client import DeliveryAttempt, DeliveryDisposition

KEY = b"K" * 32
MEETING_ID = "11111111-1111-4111-8111-111111111111"


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


def test_payload_matches_backend_contract_and_preserves_grounding(tmp_path: Path) -> None:
    payload = build_ingestion_payload(
        settings=_settings(tmp_path / "outbox.sqlite3"),
        meeting_id=MEETING_ID,
        session_id="session-1",
        transcript="Bütçe onaylandı.",
        result=_result(),
        generated_at=datetime(2026, 7, 11, 20, 0, tzinfo=UTC),
    )
    assert payload["meeting_id"] == MEETING_ID
    assert payload["transcript_session_id"] == "session-1"
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
    }
    assert "transcript" not in payload


def test_valid_iso_due_is_normalized_to_utc(tmp_path: Path) -> None:
    result = _result()
    result.action_items[0].due_date = "2026-07-12T12:00:00+03:00"
    payload = build_ingestion_payload(
        settings=_settings(tmp_path / "outbox.sqlite3"),
        meeting_id=MEETING_ID,
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
