"""Durable Redis Streams consumer for canonical transcript-ready events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import socket
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from app.api.metrics import (
    mai_ready_consumer_events_total,
    mai_ready_consumer_inbox_depth,
    mai_ready_consumer_oldest_unfinished_age_seconds,
)
from app.core.config import Settings
from app.models.ready_event import (
    ParsedTranscriptReadyEvent,
    ReadyEventContractError,
    parse_transcript_ready_event,
    payload_sha256,
    stream_event_type,
)
from app.models.schemas import AnalyzeResponse, ReadyConsumerHealth
from app.services.analysis_application import (
    AnalysisApplicationService,
    AnalysisCommand,
    AnalysisTimeoutError,
    AnalysisTranscriptTooLargeError,
)
from app.services.analysis_delivery import (
    AnalysisDeliveryContractError,
    AnalysisDeliveryRuntime,
    build_ingestion_payload,
)
from app.services.analyze import BackendUnavailableError
from app.services.canonical_transcript_client import (
    CanonicalTranscriptPort,
    CanonicalTranscriptRetryableError,
    CanonicalTranscriptTerminalError,
    HttpCanonicalTranscriptClient,
)
from app.services.durable_outbox import (
    OutboxConflictError,
    OutboxError,
    OutboxFullError,
)
from app.services.ready_event_inbox import (
    ReadyClaim,
    ReadyClaimDisposition,
    ReadyDeadReason,
    ReadyEventIdentity,
    ReadyInboxError,
    ReadyInboxFullError,
    ReadyInboxLeaseLostError,
    ReadyInboxState,
    SqliteReadyEventInbox,
)
from app.services.redact import RedactionError

logger = logging.getLogger(__name__)


class AsyncRedisClient(Protocol):
    async def xgroup_create(self, **kwargs: object) -> object: ...

    async def xreadgroup(self, **kwargs: object) -> object: ...

    async def xautoclaim(self, **kwargs: object) -> object: ...

    async def xadd(self, **kwargs: object) -> object: ...

    async def xack(self, *args: object) -> object: ...

    async def aclose(self) -> None: ...


class ReadyEventConsumerRuntime:
    """Own Redis PEL processing while SQLite owns exactly-once effect state."""

    def __init__(
        self,
        settings: Settings,
        application: AnalysisApplicationService,
        delivery: AnalysisDeliveryRuntime,
        *,
        redis_client: AsyncRedisClient | None = None,
        transcript_client: CanonicalTranscriptPort | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.enabled = settings.ready_consumer_enabled
        self._application = application
        self._delivery = delivery
        self._redis = redis_client
        self._transcripts = transcript_client
        self._owns_redis = False
        self._owns_transcripts = False
        self._random = random_source or random.Random()  # noqa: S311 - retry jitter only
        configured_name = settings.ready_redis_consumer_name.strip()
        owner_prefix = configured_name or socket.gethostname()
        self._owner = f"{owner_prefix}-{uuid.uuid4()}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._group_ready = False
        self._last_error_code: str | None = None
        self._inbox: SqliteReadyEventInbox | None = None
        self._autoclaim_cursor = "0-0"
        self._owned_pending_cursor = "0-0"
        self._next_owned_pending_scan_monotonic = 0.0
        self._next_prune_monotonic = 0.0

        if self.enabled:
            if delivery.store is None:
                raise ReadyInboxError("ready consumer requires the durable result outbox")
            self._inbox = SqliteReadyEventInbox(
                delivery.store,
                max_rows=settings.ready_consumer_inbox_max_rows,
                max_failures=settings.ready_consumer_max_failures,
            )
            if self._redis is None:
                self._redis = _build_redis_client(settings)
                self._owns_redis = True
            if self._transcripts is None:
                self._transcripts = HttpCanonicalTranscriptClient(settings)
                self._owns_transcripts = True

    @property
    def worker_running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def start(self) -> None:
        if not self.enabled or self.worker_running:
            return
        self._stop.clear()
        self._worker = asyncio.create_task(
            self._worker_loop(),
            name="meeting-transcript-ready-consumer",
        )

    async def stop(self) -> None:
        if self._worker is not None:
            self._stop.set()
            self._wake.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._worker),
                    timeout=self.settings.ready_consumer_shutdown_grace_sec,
                )
            except TimeoutError:
                self._worker.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(self._worker, return_exceptions=True),
                        timeout=min(1.0, self.settings.ready_consumer_shutdown_grace_sec),
                    )
                except TimeoutError:
                    logger.error("Ready-event worker did not acknowledge cancellation")
            self._worker = None
        if self._owns_transcripts and self._transcripts is not None:
            await self._transcripts.aclose()
        if self._owns_redis and self._redis is not None:
            await self._redis.aclose()

    async def health(self) -> ReadyConsumerHealth:
        if not self.enabled:
            return ReadyConsumerHealth(
                enabled=False,
                ready=True,
                status="disabled",
                worker_running=False,
                redis_group_ready=False,
                received=0,
                processing=0,
                outboxed=0,
                dead_letter=0,
                oldest_unfinished_age_sec=None,
                error_code=None,
            )
        assert self._inbox is not None
        try:
            summary = await asyncio.to_thread(self._inbox.summary)
        except (ReadyInboxError, sqlite3.Error, OSError) as exc:
            return ReadyConsumerHealth(
                enabled=True,
                ready=False,
                status="degraded",
                worker_running=self.worker_running,
                redis_group_ready=self._group_ready,
                received=0,
                processing=0,
                outboxed=0,
                dead_letter=0,
                oldest_unfinished_age_sec=None,
                error_code=type(exc).__name__,
            )
        degraded = (
            not self.worker_running
            or not self._group_ready
            or self._last_error_code is not None
            or summary.dead > 0
            or (
                summary.oldest_unfinished_age_sec is not None
                and summary.oldest_unfinished_age_sec > self.settings.ready_consumer_stale_after_sec
            )
        )
        return ReadyConsumerHealth(
            enabled=True,
            ready=(self.worker_running and self._group_ready and self._last_error_code is None),
            status="degraded" if degraded else "ok",
            worker_running=self.worker_running,
            redis_group_ready=self._group_ready,
            received=summary.received,
            processing=summary.processing,
            outboxed=summary.outboxed,
            dead_letter=summary.dead,
            oldest_unfinished_age_sec=summary.oldest_unfinished_age_sec,
            error_code=self._last_error_code,
        )

    async def process_message(self, message_id: str, fields: dict[object, object]) -> None:
        """Process one PEL record; never acknowledge before durable terminal state."""
        assert self._inbox is not None
        outer_event_type = stream_event_type(fields)
        if outer_event_type is not None and outer_event_type != "meeting.transcript.ready":
            await self._ack(message_id)
            mai_ready_consumer_events_total.labels(outcome="ignored").inc()
            return
        try:
            event = parse_transcript_ready_event(
                fields,
                analysis_spec_version=self.settings.analysis_spec_version,
            )
        except (ReadyEventContractError, ValidationError, UnicodeDecodeError):
            synthetic_key, published = await asyncio.to_thread(
                self._inbox.record_poison,
                source_message_id=message_id,
                payload_sha256=payload_sha256(fields),
                error_code="event_contract_invalid",
            )
            await self._dead_letter_and_ack(
                message_id=message_id,
                event_key=synthetic_key,
                lookup_key=synthetic_key,
                error_code="event_contract_invalid",
                failure_count=0,
                already_published=published,
            )
            mai_ready_consumer_events_total.labels(outcome="invalid").inc()
            return

        identity = ReadyEventIdentity(
            event_key=event.event_key,
            payload_sha256=event.payload_sha256,
            tenant_id=str(event.tenant_id),
            meeting_id=str(event.meeting_id),
            session_id=str(event.session_id),
            finalization_version=event.finalization_version,
            analysis_run_id=str(event.analysis_run_id),
        )
        claim = await asyncio.to_thread(
            self._inbox.register_and_claim,
            identity,
            owner=self._owner,
            lease_sec=self.settings.ready_consumer_lease_sec,
        )
        if claim.disposition is ReadyClaimDisposition.BUSY:
            self._schedule_owned_pending_scan(claim.retry_after_sec)
            mai_ready_consumer_events_total.labels(outcome="busy").inc()
            return
        if claim.disposition is ReadyClaimDisposition.OUTBOXED:
            await self._ack(message_id)
            mai_ready_consumer_events_total.labels(outcome="duplicate").inc()
            return
        if claim.disposition in {ReadyClaimDisposition.DEAD, ReadyClaimDisposition.CONFLICT}:
            error_code = (
                "event_payload_hash_conflict"
                if claim.disposition is ReadyClaimDisposition.CONFLICT
                else "event_previously_dead"
            )
            await self._dead_letter_and_ack(
                message_id=message_id,
                event_key=event.event_key,
                lookup_key=event.lookup_key,
                error_code=error_code,
                failure_count=claim.failure_count,
                already_published=claim.dlq_published,
            )
            mai_ready_consumer_events_total.labels(outcome="dead_letter").inc()
            return

        assert claim.lease_token is not None
        assert claim.analysis_run_id is not None
        await self._process_claimed(message_id, event, claim)

    async def _process_claimed(
        self,
        message_id: str,
        event: ParsedTranscriptReadyEvent,
        claim: ReadyClaim,
    ) -> None:
        assert self._transcripts is not None
        lease_token = claim.lease_token
        assert lease_token is not None
        analysis_run_id = claim.analysis_run_id
        assert analysis_run_id is not None
        try:
            snapshot = await self._transcripts.fetch(event)
            segments = [segment.model_dump() for segment in snapshot.segments]

            async def persist_result(
                command: AnalysisCommand,
                result: AnalyzeResponse,
            ) -> str | None:
                assert self._inbox is not None
                assert command.analysis_run_id is not None
                payload = build_ingestion_payload(
                    settings=self.settings,
                    meeting_id=str(event.meeting_id),
                    tenant_id=str(event.tenant_id),
                    session_id=str(event.session_id),
                    finalization_version=snapshot.finalization_version,
                    finalized_at=snapshot.finalized_at,
                    analysis_spec_version=self.settings.analysis_spec_version,
                    transcript=command.transcript,
                    result=result,
                    generated_at=datetime.now(UTC),
                )
                await asyncio.to_thread(
                    self._inbox.commit_outboxed,
                    lookup_key=event.lookup_key,
                    owner=self._owner,
                    lease_token=lease_token,
                    analysis_run_id=command.analysis_run_id,
                    meeting_id=str(event.meeting_id),
                    payload=payload,
                )
                self._delivery.notify_outbox_work()
                return command.analysis_run_id

            await self._application.execute(
                AnalysisCommand(
                    transcript=snapshot.transcript,
                    meeting_id=str(event.meeting_id),
                    session_id=str(event.session_id),
                    finalization_version=event.finalization_version,
                    analysis_run_id=analysis_run_id,
                    segments=segments,
                ),
                persist=persist_result,
            )
        except CanonicalTranscriptRetryableError as exc:
            await self._retry_or_dead(
                message_id,
                event,
                claim,
                error_code=exc.error_code,
                retry_after_sec=exc.retry_after_sec,
            )
            return
        except OutboxFullError:
            await self._defer_for_backpressure(
                event,
                claim,
                error_code="store_OutboxFullError",
            )
            return
        except (
            BackendUnavailableError,
            AnalysisTimeoutError,
            OSError,
            sqlite3.Error,
        ) as exc:
            await self._retry_or_dead(
                message_id,
                event,
                claim,
                error_code=f"processing_{type(exc).__name__}",
            )
            return
        except ReadyInboxLeaseLostError:
            mai_ready_consumer_events_total.labels(outcome="lease_lost").inc()
            return
        except OutboxError as exc:
            if isinstance(exc, OutboxConflictError):
                await self._terminal(
                    message_id,
                    event,
                    claim,
                    error_code="analysis_run_payload_conflict",
                    failure_count=claim.failure_count,
                )
            else:
                await self._retry_or_dead(
                    message_id,
                    event,
                    claim,
                    error_code=f"store_{type(exc).__name__}",
                )
            return
        except (
            CanonicalTranscriptTerminalError,
            AnalysisTranscriptTooLargeError,
            AnalysisDeliveryContractError,
            RedactionError,
            NotImplementedError,
        ) as exc:
            error_code = getattr(exc, "error_code", f"processing_{type(exc).__name__}")
            await self._terminal(
                message_id,
                event,
                claim,
                error_code=str(error_code),
                failure_count=claim.failure_count,
            )
            return
        except Exception as exc:  # noqa: BLE001 - persist bounded retry without exception text
            logger.error(
                "Unexpected ready-event processing failure",
                extra={
                    "event_key_sha256": hashlib.sha256(event.event_key.encode()).hexdigest(),
                    "err_class": type(exc).__name__,
                },
            )
            await self._retry_or_dead(
                message_id,
                event,
                claim,
                error_code=f"unexpected_{type(exc).__name__}",
            )
            return

        await self._ack(message_id)
        mai_ready_consumer_events_total.labels(outcome="outboxed").inc()
        await self._refresh_metrics()

    async def _retry_or_dead(
        self,
        message_id: str,
        event: ParsedTranscriptReadyEvent,
        claim: ReadyClaim,
        *,
        error_code: str,
        retry_after_sec: float | None = None,
    ) -> None:
        assert self._inbox is not None
        assert claim.lease_token is not None
        delay = self._retry_delay(claim.failure_count + 1, retry_after_sec)
        try:
            outcome = await asyncio.to_thread(
                self._inbox.mark_failure,
                lookup_key=event.lookup_key,
                owner=self._owner,
                lease_token=claim.lease_token,
                error_code=error_code,
                next_attempt_at=time.time() + delay,
                max_failures=self.settings.ready_consumer_max_failures,
            )
        except ReadyInboxLeaseLostError:
            mai_ready_consumer_events_total.labels(outcome="lease_lost").inc()
            return
        if outcome.state is ReadyInboxState.DEAD:
            await self._dead_letter_and_ack(
                message_id=message_id,
                event_key=event.event_key,
                lookup_key=event.lookup_key,
                error_code=error_code,
                failure_count=outcome.failure_count,
                already_published=False,
            )
            mai_ready_consumer_events_total.labels(outcome="dead_letter").inc()
        else:
            self._schedule_owned_pending_scan(delay)
            mai_ready_consumer_events_total.labels(outcome="retry_scheduled").inc()
        await self._refresh_metrics()

    async def _terminal(
        self,
        message_id: str,
        event: ParsedTranscriptReadyEvent,
        claim: ReadyClaim,
        *,
        error_code: str,
        failure_count: int,
    ) -> None:
        assert self._inbox is not None
        assert claim.lease_token is not None
        try:
            await asyncio.to_thread(
                self._inbox.mark_dead,
                lookup_key=event.lookup_key,
                owner=self._owner,
                lease_token=claim.lease_token,
                error_code=error_code,
                reason=ReadyDeadReason.TERMINAL,
            )
        except ReadyInboxLeaseLostError:
            mai_ready_consumer_events_total.labels(outcome="lease_lost").inc()
            return
        await self._dead_letter_and_ack(
            message_id=message_id,
            event_key=event.event_key,
            lookup_key=event.lookup_key,
            error_code=error_code,
            failure_count=failure_count,
            already_published=False,
        )
        mai_ready_consumer_events_total.labels(outcome="dead_letter").inc()
        await self._refresh_metrics()

    async def _defer_for_backpressure(
        self,
        event: ParsedTranscriptReadyEvent,
        claim: ReadyClaim,
        *,
        error_code: str,
    ) -> None:
        """Keep the Redis PEL entry without treating local capacity as event failure."""
        assert self._inbox is not None
        assert claim.lease_token is not None
        try:
            await asyncio.to_thread(
                self._inbox.defer_without_failure,
                lookup_key=event.lookup_key,
                owner=self._owner,
                lease_token=claim.lease_token,
                error_code=error_code,
                next_attempt_at=time.time() + self.settings.ready_consumer_base_backoff_sec,
            )
        except ReadyInboxLeaseLostError:
            mai_ready_consumer_events_total.labels(outcome="lease_lost").inc()
            return
        self._last_error_code = error_code
        self._next_prune_monotonic = 0.0
        self._schedule_owned_pending_scan(self.settings.ready_consumer_base_backoff_sec)
        mai_ready_consumer_events_total.labels(outcome="backpressure").inc()
        await self._refresh_metrics()

    async def _dead_letter_and_ack(
        self,
        *,
        message_id: str,
        event_key: str,
        lookup_key: str,
        error_code: str,
        failure_count: int,
        already_published: bool,
    ) -> None:
        assert self._redis is not None
        assert self._inbox is not None
        if not already_published:
            await self._redis.xadd(
                name=self.settings.ready_redis_dead_letter_stream,
                fields={
                    "dlqKey": hashlib.sha256(
                        f"{message_id}|{error_code[:128]}".encode()
                    ).hexdigest(),
                    "sourceMessageId": message_id,
                    "eventKey": event_key,
                    "errorCode": error_code[:128],
                    "failureCount": str(failure_count),
                },
                maxlen=self.settings.ready_redis_dead_letter_maxlen,
                approximate=True,
            )
            await asyncio.to_thread(self._inbox.mark_dlq_published, lookup_key)
        await self._ack(message_id)

    async def _ack(self, message_id: str) -> None:
        assert self._redis is not None
        await self._redis.xack(
            self.settings.ready_redis_stream,
            self.settings.ready_redis_group,
            message_id,
        )

    async def _worker_loop(self) -> None:
        assert self._redis is not None
        while not self._stop.is_set():
            try:
                if not self._group_ready:
                    await self._ensure_group()
                await self._maybe_prune()
                owned_pending = await self._read_owned_pending()
                if owned_pending:
                    for message_id, fields in owned_pending:
                        await self.process_message(message_id, fields)
                    self._clear_redis_error()
                    continue
                pending = await self._claim_stale()
                if pending:
                    for message_id, fields in pending:
                        await self.process_message(message_id, fields)
                    self._clear_redis_error()
                    continue
                records = await self._redis.xreadgroup(
                    groupname=self.settings.ready_redis_group,
                    consumername=self._owner,
                    streams={self.settings.ready_redis_stream: ">"},
                    count=self.settings.ready_redis_batch_size,
                    block=self._new_event_block_ms(),
                )
                for _stream, messages in _normalize_records(records):
                    for message_id, fields in messages:
                        await self.process_message(message_id, fields)
                self._clear_redis_error()
                await self._refresh_metrics()
            except asyncio.CancelledError:
                raise
            except ReadyInboxFullError as exc:
                self._last_error_code = f"store_{type(exc).__name__}"
                self._next_prune_monotonic = 0.0
                logger.error(
                    "Ready-event inbox capacity reached; message remains pending",
                    extra={"err_class": type(exc).__name__},
                )
                await self._wait_after_error()
            except (ReadyInboxError, sqlite3.Error, OSError) as exc:
                self._last_error_code = f"store_{type(exc).__name__}"
                logger.error(
                    "Ready-event durable store failure; message remains pending",
                    extra={"err_class": type(exc).__name__},
                )
                await self._wait_after_error()
            except Exception as exc:  # noqa: BLE001 - outer worker boundary must stay alive
                self._group_ready = False
                self._last_error_code = f"redis_{type(exc).__name__}"
                logger.error(
                    "Ready-event consumer loop failure",
                    extra={"err_class": type(exc).__name__},
                )
                await self._wait_after_error()

    async def _ensure_group(self) -> None:
        assert self._redis is not None
        try:
            await self._redis.xgroup_create(
                name=self.settings.ready_redis_stream,
                groupname=self.settings.ready_redis_group,
                id="0-0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def _claim_stale(self) -> list[tuple[str, dict[object, object]]]:
        assert self._redis is not None
        assert self._inbox is not None
        result = await self._redis.xautoclaim(
            name=self.settings.ready_redis_stream,
            groupname=self.settings.ready_redis_group,
            consumername=self._owner,
            min_idle_time=self.settings.ready_redis_claim_idle_ms,
            start_id=self._autoclaim_cursor,
            count=self.settings.ready_redis_batch_size,
        )
        cursor, messages, deleted_ids = _normalize_autoclaim(result)
        self._autoclaim_cursor = cursor
        for source_message_id in deleted_ids:
            synthetic_key, published = await asyncio.to_thread(
                self._inbox.record_poison,
                source_message_id=source_message_id,
                payload_sha256=hashlib.sha256(b"").hexdigest(),
                error_code="redis_pending_source_deleted",
            )
            await self._dead_letter_and_ack(
                message_id=source_message_id,
                event_key=synthetic_key,
                lookup_key=synthetic_key,
                error_code="redis_pending_source_deleted",
                failure_count=0,
                already_published=published,
            )
            mai_ready_consumer_events_total.labels(outcome="source_deleted").inc()
        return messages

    async def _read_owned_pending(self) -> list[tuple[str, dict[object, object]]]:
        """Scan this consumer's PEL so configured retry deadlines are effective."""
        assert self._redis is not None
        now = time.monotonic()
        if self._owned_pending_cursor == "0-0":
            if now < self._next_owned_pending_scan_monotonic:
                return []
            self._next_owned_pending_scan_monotonic = 0.0
        records = await self._redis.xreadgroup(
            groupname=self.settings.ready_redis_group,
            consumername=self._owner,
            streams={self.settings.ready_redis_stream: self._owned_pending_cursor},
            count=self.settings.ready_redis_batch_size,
        )
        messages = [
            message
            for _stream, stream_messages in _normalize_records(records)
            for message in stream_messages
        ]
        if messages:
            self._owned_pending_cursor = messages[-1][0]
        elif self._owned_pending_cursor != "0-0":
            self._owned_pending_cursor = "0-0"
        if not messages and self._next_owned_pending_scan_monotonic == 0.0:
            self._schedule_owned_pending_scan(self.settings.ready_redis_block_ms / 1000)
        return messages

    def _schedule_owned_pending_scan(self, delay_sec: float | None) -> None:
        delay = (
            self.settings.ready_redis_block_ms / 1000 if delay_sec is None else max(0.01, delay_sec)
        )
        deadline = time.monotonic() + delay
        if (
            self._next_owned_pending_scan_monotonic == 0.0
            or deadline < self._next_owned_pending_scan_monotonic
        ):
            self._next_owned_pending_scan_monotonic = deadline

    def _new_event_block_ms(self) -> int:
        if self._next_owned_pending_scan_monotonic == 0.0:
            return self.settings.ready_redis_block_ms
        remaining_ms = int(
            max(0.001, self._next_owned_pending_scan_monotonic - time.monotonic()) * 1000
        )
        return max(1, min(self.settings.ready_redis_block_ms, remaining_ms))

    async def _maybe_prune(self) -> None:
        assert self._inbox is not None
        now = time.monotonic()
        if now < self._next_prune_monotonic:
            return
        await asyncio.to_thread(
            self._inbox.prune_terminal,
            retention_sec=self.settings.ready_consumer_retention_sec,
            batch_size=self.settings.ready_consumer_prune_batch_size,
        )
        store_alarm = self._last_error_code is not None and self._last_error_code.startswith(
            "store_"
        )
        capacities_available = await self._store_capacities_available()
        if store_alarm and capacities_available:
            self._last_error_code = None
        interval = (
            self.settings.ready_consumer_prune_interval_sec
            if capacities_available
            else self.settings.ready_consumer_base_backoff_sec
        )
        self._next_prune_monotonic = now + interval

    async def _store_capacities_available(self) -> bool:
        assert self._inbox is not None
        result_store = self._delivery.store
        if result_store is None:
            return False
        inbox_available, outbox_available = await asyncio.gather(
            asyncio.to_thread(self._inbox.has_capacity),
            asyncio.to_thread(result_store.has_capacity),
        )
        return inbox_available and outbox_available

    def _clear_redis_error(self) -> None:
        if self._last_error_code is not None and self._last_error_code.startswith("redis_"):
            self._last_error_code = None

    def _retry_delay(self, failure_count: int, retry_after_sec: float | None) -> float:
        exponential = min(
            self.settings.ready_consumer_max_backoff_sec,
            self.settings.ready_consumer_base_backoff_sec * (2 ** (failure_count - 1)),
        )
        jitter = self.settings.ready_consumer_jitter_ratio
        delay = min(
            self.settings.ready_consumer_max_backoff_sec,
            exponential * self._random.uniform(1.0 - jitter, 1.0 + jitter),
        )
        if retry_after_sec is not None:
            delay = max(delay, retry_after_sec)
        return float(min(self.settings.ready_consumer_max_backoff_sec, delay))

    async def _wait_after_error(self) -> None:
        self._wake.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._wake.wait(),
                timeout=self.settings.ready_consumer_base_backoff_sec,
            )

    async def _refresh_metrics(self) -> None:
        if self._inbox is None:
            return
        try:
            summary = await asyncio.to_thread(self._inbox.summary)
        except (ReadyInboxError, sqlite3.Error, OSError):
            return
        for state, value in (
            ("received", summary.received),
            ("processing", summary.processing),
            ("outboxed", summary.outboxed),
            ("dead", summary.dead),
        ):
            mai_ready_consumer_inbox_depth.labels(state=state).set(value)
        mai_ready_consumer_oldest_unfinished_age_seconds.set(
            summary.oldest_unfinished_age_sec or 0.0
        )


def _build_redis_client(settings: Settings) -> Any:
    from redis.asyncio import Redis

    return Redis.from_url(
        settings.ready_redis_url.get_secret_value(),
        decode_responses=False,
        socket_connect_timeout=settings.ready_redis_connect_timeout_sec,
        socket_timeout=settings.ready_redis_command_timeout_sec,
        health_check_interval=30,
        retry_on_timeout=False,
    )


def _normalize_records(
    value: object,
) -> list[tuple[str, list[tuple[str, dict[object, object]]]]]:
    if not isinstance(value, list | tuple):
        return []
    normalized: list[tuple[str, list[tuple[str, dict[object, object]]]]] = []
    for record in value:
        if not isinstance(record, list | tuple) or len(record) != 2:
            continue
        stream, raw_messages = record
        messages = _normalize_messages(raw_messages)
        normalized.append((_text(stream), messages))
    return normalized


def _normalize_autoclaim(
    value: object,
) -> tuple[str, list[tuple[str, dict[object, object]]], list[str]]:
    if not isinstance(value, list | tuple) or len(value) < 2:
        return "0-0", [], []
    deleted_ids = (
        [_text(item) for item in value[2]]
        if len(value) >= 3 and isinstance(value[2], list | tuple)
        else []
    )
    return _text(value[0]), _normalize_messages(value[1]), deleted_ids


def _normalize_messages(value: object) -> list[tuple[str, dict[object, object]]]:
    if not isinstance(value, list | tuple):
        return []
    messages: list[tuple[str, dict[object, object]]] = []
    for item in value:
        if not isinstance(item, list | tuple) or len(item) != 2 or not isinstance(item[1], dict):
            continue
        messages.append((_text(item[0]), item[1]))
    return messages


def _text(value: object) -> str:
    return value.decode("utf-8", errors="strict") if isinstance(value, bytes) else str(value)
