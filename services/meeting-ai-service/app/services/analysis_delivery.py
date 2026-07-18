"""Durable, non-blocking analysis-result handoff to meeting-service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol

import httpx

from app.api.metrics import (
    mai_ingestion_delivery_total,
    mai_ingestion_enqueue_total,
    mai_ingestion_oldest_pending_age_seconds,
    mai_ingestion_queue_depth,
)
from app.core.config import Settings
from app.models.schemas import AnalysisDeliveryHealth, AnalyzeResponse
from app.services.durable_outbox import (
    ClaimedMessage,
    OutboxError,
    OutboxFullError,
    OutboxIntegrityError,
    OutboxSummary,
    PayloadCipher,
    SqliteOutboxStore,
)
from app.services.meeting_service_client import (
    DeliveryAttempt,
    DeliveryDisposition,
    MeetingServiceClient,
)

logger = logging.getLogger(__name__)

_GROUNDING_STATUSES = {"verified", "partial_verified", "withheld", "empty"}


class AnalysisTransport(Protocol):
    async def deliver(self, message: ClaimedMessage) -> DeliveryAttempt: ...


class AnalysisDeliveryContractError(ValueError):
    """The request cannot be bound to the canonical persistence contract."""


def build_ingestion_payload(
    *,
    settings: Settings,
    meeting_id: str,
    session_id: str,
    transcript: str,
    result: AnalyzeResponse,
    generated_at: datetime,
) -> dict[str, object]:
    """Map the v5 AI response to backend BE-1c without storing raw transcript."""
    try:
        canonical_meeting_id = str(uuid.UUID(meeting_id))
    except ValueError as exc:
        raise AnalysisDeliveryContractError("meeting_id must be a UUID") from exc
    if not session_id.strip():
        raise AnalysisDeliveryContractError("session_id is required for durable delivery")
    if len(session_id) > 64:
        raise AnalysisDeliveryContractError("session_id exceeds the backend contract limit")
    _validate_backend_contract(settings, result)

    return {
        "meeting_id": canonical_meeting_id,
        "transcript_session_id": session_id,
        "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        "analyzer_contract_version": result.schema_version,
        "model": result.model,
        "backend": result.backend,
        "prompt_version": settings.effective_prompt_version,
        "summary": result.summary,
        "summary_grounding_status": result.summary_grounding_status,
        "summary_citations": [item.model_dump() for item in result.summary_citations],
        "citations": [item.model_dump() for item in result.citations],
        "rejected_claims": [item.model_dump() for item in result.rejected_claims],
        "ungrounded_count": result.ungrounded_count,
        "redacted": result.redacted,
        "redaction_count": result.redaction_count,
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "decisions": result.decisions,
        "actions": [_action_payload(item.model_dump()) for item in result.action_items],
        "supersedes_analysis_run_id": None,
    }


def _validate_backend_contract(settings: Settings, result: AnalyzeResponse) -> None:
    if not result.redacted:
        raise AnalysisDeliveryContractError(
            "durable delivery requires an analysis produced through the redaction boundary"
        )
    if result.summary_grounding_status not in _GROUNDING_STATUSES:
        raise AnalysisDeliveryContractError("unsupported summary_grounding_status")
    if len(result.schema_version) > 64 or len(result.model) > 128 or len(result.backend) > 64:
        raise AnalysisDeliveryContractError("analysis provenance exceeds backend contract limits")
    if len(settings.effective_prompt_version) > 64 or len(result.summary) > 200_000:
        raise AnalysisDeliveryContractError("analysis output exceeds backend contract limits")
    if len(result.decisions) > 1000 or any(
        not item.strip() or len(item) > 4000 for item in result.decisions
    ):
        raise AnalysisDeliveryContractError("decisions exceed backend contract limits")
    if len(result.action_items) > 1000 or any(
        not item.text.strip()
        or len(item.text) > 2000
        or (item.owner is not None and len(item.owner) > 255)
        for item in result.action_items
    ):
        raise AnalysisDeliveryContractError("actions exceed backend contract limits")
    if (
        len(result.summary_citations) > 2000
        or len(result.citations) > 2000
        or len(result.rejected_claims) > 2000
    ):
        raise AnalysisDeliveryContractError("citation evidence exceeds backend contract limits")


def _action_payload(item: dict[str, object]) -> dict[str, object]:
    # Backend accepts an Instant. Relative phrases such as "cuma" remain useful
    # in the grounded response but cannot be truthfully coerced into a timestamp.
    return {
        "text": item.get("text"),
        "assignee": item.get("owner"),
        "due": _strict_iso_instant(item.get("due_date")),
    }


def _strict_iso_instant(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    normalized = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AnalysisDeliveryRuntime:
    """Own the encrypted store, delivery worker, and runtime health state."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: SqliteOutboxStore | None = None,
        transport: AnalysisTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.enabled = settings.ingestion_enabled
        self._owns_transport = False
        self._store = store
        self._transport = transport
        self._random = random_source or random.Random()  # noqa: S311 - retry jitter, not crypto
        self._owner = f"meeting-ai-{uuid.uuid4()}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

        if self.enabled and self._store is None:
            cipher = PayloadCipher(
                settings.ingestion_encryption_keys(),
                settings.ingestion_active_key_id,
            )
            self._store = SqliteOutboxStore(
                settings.ingestion_store_path,
                cipher,
                max_rows=settings.ingestion_max_rows,
            )
        if self.enabled and self._transport is None:
            self._transport = MeetingServiceClient(settings, http_client)
            self._owns_transport = True

    @property
    def worker_running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    @property
    def store(self) -> SqliteOutboxStore | None:
        """Shared store handle for the ready inbox's atomic commit boundary."""
        return self._store

    async def start(self) -> None:
        if not self.enabled or self.worker_running:
            return
        assert self._store is not None
        assert self._transport is not None
        self._stop.clear()
        self._worker = asyncio.create_task(self._worker_loop(), name="analysis-delivery-worker")

    async def stop(self) -> None:
        if self._worker is not None:
            self._stop.set()
            self._wake.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._worker),
                    timeout=self.settings.ingestion_shutdown_grace_sec,
                )
            except TimeoutError:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._owns_transport and isinstance(self._transport, MeetingServiceClient):
            await self._transport.aclose()

    async def enqueue_analysis(
        self,
        *,
        meeting_id: str | None,
        session_id: str | None,
        transcript: str,
        result: AnalyzeResponse,
        analysis_run_id: str | None = None,
        generated_at: datetime | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        if not meeting_id or not session_id:
            mai_ingestion_enqueue_total.labels(outcome="invalid_contract").inc()
            raise AnalysisDeliveryContractError(
                "meeting_id and session_id are required when durable delivery is enabled"
            )
        run_id = analysis_run_id or str(uuid.uuid4())
        payload = build_ingestion_payload(
            settings=self.settings,
            meeting_id=meeting_id,
            session_id=session_id,
            transcript=transcript,
            result=result,
            generated_at=generated_at or datetime.now(UTC),
        )
        assert self._store is not None
        try:
            await asyncio.to_thread(
                self._store.enqueue,
                analysis_run_id=run_id,
                meeting_id=str(payload["meeting_id"]),
                payload=payload,
            )
        except OutboxFullError:
            mai_ingestion_enqueue_total.labels(outcome="full").inc()
            raise
        except OutboxError:
            mai_ingestion_enqueue_total.labels(outcome="error").inc()
            raise
        except (sqlite3.Error, OSError) as exc:
            mai_ingestion_enqueue_total.labels(outcome="error").inc()
            raise OutboxError("durable analysis outbox is unavailable") from exc
        mai_ingestion_enqueue_total.labels(outcome="accepted").inc()
        self._wake.set()
        await self._refresh_metrics()
        return run_id

    def notify_outbox_work(self) -> None:
        """Wake result delivery after another component commits into the shared outbox."""
        self._wake.set()

    async def health(self) -> AnalysisDeliveryHealth:
        if not self.enabled:
            return AnalysisDeliveryHealth(
                enabled=False,
                ready=True,
                status="disabled",
                worker_running=False,
                pending=0,
                in_flight=0,
                dead_letter=0,
                oldest_pending_age_sec=None,
                error_code=None,
            )
        assert self._store is not None
        try:
            summary = await asyncio.to_thread(self._store.summary)
            has_capacity = await asyncio.to_thread(self._store.has_capacity)
        except (OutboxError, sqlite3.Error, OSError) as exc:
            return AnalysisDeliveryHealth(
                enabled=True,
                ready=False,
                status="degraded",
                worker_running=self.worker_running,
                pending=0,
                in_flight=0,
                dead_letter=0,
                oldest_pending_age_sec=None,
                error_code=type(exc).__name__,
            )
        degraded = (
            not self.worker_running
            or not has_capacity
            or summary.dead > 0
            or (
                summary.oldest_pending_age_sec is not None
                and summary.oldest_pending_age_sec > self.settings.ingestion_stale_after_sec
            )
        )
        return AnalysisDeliveryHealth(
            enabled=True,
            ready=(self.worker_running and has_capacity),
            status="degraded" if degraded else "ok",
            worker_running=self.worker_running,
            pending=summary.pending,
            in_flight=summary.in_flight,
            dead_letter=summary.dead,
            oldest_pending_age_sec=summary.oldest_pending_age_sec,
            error_code=None if has_capacity else "OutboxFullError",
        )

    async def _worker_loop(self) -> None:
        assert self._store is not None
        assert self._transport is not None
        while not self._stop.is_set():
            try:
                message = await asyncio.to_thread(
                    self._store.claim_next,
                    owner=self._owner,
                    lease_sec=self.settings.ingestion_lease_sec,
                )
            except (OutboxError, sqlite3.Error, OSError) as exc:
                logger.exception(
                    "Analysis outbox claim failure",
                    extra={"err_class": type(exc).__name__},
                )
                outcome = (
                    "integrity_error" if isinstance(exc, OutboxIntegrityError) else "store_error"
                )
                mai_ingestion_delivery_total.labels(outcome=outcome).inc()
                await self._wait_for_work()
                continue
            if message is None:
                await self._refresh_metrics()
                await self._wait_for_work()
                continue
            await self._deliver(message)
            await self._refresh_metrics()

    async def _deliver(self, message: ClaimedMessage) -> None:
        assert self._store is not None
        assert self._transport is not None
        try:
            outcome = await self._transport.deliver(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected analysis delivery adapter failure",
                extra={
                    "analysis_run_id": message.analysis_run_id,
                    "meeting_id": message.meeting_id,
                    "err_class": type(exc).__name__,
                },
            )
            outcome = DeliveryAttempt(
                DeliveryDisposition.RETRY,
                error_code=f"adapter_{type(exc).__name__}",
            )

        if outcome.disposition in (
            DeliveryDisposition.DELIVERED,
            DeliveryDisposition.REPLAYED,
        ):
            await asyncio.to_thread(
                self._store.mark_delivered,
                analysis_run_id=message.analysis_run_id,
                owner=self._owner,
            )
            mai_ingestion_delivery_total.labels(outcome=outcome.disposition.value).inc()
            return

        if (
            outcome.disposition is DeliveryDisposition.TERMINAL
            or message.attempt_count >= self.settings.ingestion_max_attempts
        ):
            await asyncio.to_thread(
                self._store.mark_dead,
                analysis_run_id=message.analysis_run_id,
                owner=self._owner,
                error_code=outcome.error_code or "max_attempts",
            )
            mai_ingestion_delivery_total.labels(outcome="dead_letter").inc()
            return

        next_attempt = time.time() + self._retry_delay(message, outcome)
        await asyncio.to_thread(
            self._store.mark_retry,
            analysis_run_id=message.analysis_run_id,
            owner=self._owner,
            next_attempt_at=next_attempt,
            error_code=outcome.error_code or "retryable",
        )
        mai_ingestion_delivery_total.labels(outcome="retry_scheduled").inc()

    def _retry_delay(self, message: ClaimedMessage, outcome: DeliveryAttempt) -> float:
        exponential = min(
            self.settings.ingestion_max_backoff_sec,
            self.settings.ingestion_base_backoff_sec * (2 ** (message.attempt_count - 1)),
        )
        jitter = self.settings.ingestion_jitter_ratio
        maximum = self.settings.ingestion_max_backoff_sec
        jittered = min(
            maximum,
            float(exponential * self._random.uniform(1.0 - jitter, 1.0 + jitter)),
        )
        if outcome.retry_after_sec is not None:
            return float(min(maximum, max(jittered, outcome.retry_after_sec)))
        return float(jittered)

    async def _wait_for_work(self) -> None:
        self._wake.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._wake.wait(),
                timeout=self.settings.ingestion_poll_interval_sec,
            )

    async def _refresh_metrics(self) -> OutboxSummary | None:
        if not self.enabled or self._store is None:
            return None
        try:
            summary = await asyncio.to_thread(self._store.summary)
        except (OutboxError, sqlite3.Error, OSError) as exc:
            logger.exception(
                "Analysis outbox metrics refresh failure",
                extra={"err_class": type(exc).__name__},
            )
            return None
        mai_ingestion_queue_depth.labels(state="pending").set(summary.pending)
        mai_ingestion_queue_depth.labels(state="in_flight").set(summary.in_flight)
        mai_ingestion_queue_depth.labels(state="dead_letter").set(summary.dead)
        mai_ingestion_oldest_pending_age_seconds.set(summary.oldest_pending_age_sec or 0.0)
        return summary
