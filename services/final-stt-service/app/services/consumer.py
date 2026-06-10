"""Redis Streams consumer for final STT jobs."""

from __future__ import annotations

import threading
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from app.api.metrics import (
    final_stt_chunk_duration_seconds,
    final_stt_consumer_up,
    final_stt_inference_seconds,
    final_stt_jobs_total,
)
from app.core.config import Settings
from app.models.schemas import FinalSttJob
from app.services.transcribe import FinalTranscriber

logger = structlog.get_logger(__name__)


class RedisClient(Protocol):
    def xgroup_create(self, name: str, groupname: str, id: str, mkstream: bool) -> object: ...

    def xreadgroup(
        self, **kwargs: object
    ) -> list[tuple[object, list[tuple[object, dict[object, object]]]]]: ...

    def xadd(self, name: str, fields: dict[str, str], maxlen: int, approximate: bool) -> object: ...

    def xack(self, name: str, groupname: str, *ids: str) -> object: ...


def _decode(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _decode_fields(fields: dict[object, object]) -> dict[str, str]:
    return {_decode(key): _decode(value) for key, value in fields.items()}


class FinalSttConsumer:
    def __init__(
        self,
        settings: Settings,
        transcriber: FinalTranscriber,
        redis_client: RedisClient,
    ) -> None:
        self._settings = settings
        self._transcriber = transcriber
        self._redis = redis_client
        self._stop_event = threading.Event()

    def ensure_group(self) -> None:
        try:
            self._redis.xgroup_create(
                name=self._settings.redis_input_stream,
                groupname=self._settings.redis_consumer_group,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def stop(self) -> None:
        self._stop_event.set()

    def process_message(self, message_id: str, fields: dict[object, object]) -> None:
        try:
            job = FinalSttJob.model_validate(_decode_fields(fields))
            final_stt_chunk_duration_seconds.observe(job.audio_duration_sec)
            result = self._transcriber.transcribe(job)
            final_stt_inference_seconds.observe(result.elapsed_ms / 1000)
            self._redis.xadd(
                self._settings.redis_result_stream,
                {"payload": result.model_dump_json(by_alias=True)},
                maxlen=self._settings.redis_result_maxlen,
                approximate=True,
            )
            self._redis.xack(
                self._settings.redis_input_stream,
                self._settings.redis_consumer_group,
                message_id,
            )
            final_stt_jobs_total.labels(result="success").inc()
            logger.info(
                "final_stt_job_completed",
                correlation_id=job.correlation_id,
                chunk_seq=job.chunk_seq,
                elapsed_ms=result.elapsed_ms,
            )
        except (ValidationError, ValueError, FileNotFoundError) as exc:
            final_stt_jobs_total.labels(result="invalid").inc()
            self._redis.xadd(
                self._settings.redis_dead_letter_stream,
                {
                    "sourceMessageId": message_id,
                    "errorClass": type(exc).__name__,
                },
                maxlen=self._settings.redis_result_maxlen,
                approximate=True,
            )
            self._redis.xack(
                self._settings.redis_input_stream,
                self._settings.redis_consumer_group,
                message_id,
            )
            logger.warning(
                "final_stt_job_rejected",
                correlation_id="-",
                error_class=type(exc).__name__,
            )
        except Exception as exc:
            final_stt_jobs_total.labels(result="retry").inc()
            logger.exception(
                "final_stt_job_failed_pending",
                correlation_id="-",
                error_class=type(exc).__name__,
            )

    def run(self) -> None:
        self.ensure_group()
        final_stt_consumer_up.set(1)
        try:
            while not self._stop_event.is_set():
                records = self._redis.xreadgroup(
                    groupname=self._settings.redis_consumer_group,
                    consumername=self._settings.redis_consumer_name,
                    streams={self._settings.redis_input_stream: ">"},
                    count=self._settings.redis_batch_size,
                    block=self._settings.redis_block_ms,
                )
                for _stream, messages in records:
                    for message_id, fields in messages:
                        assert isinstance(fields, dict)
                        self.process_message(_decode(message_id), fields)
        finally:
            final_stt_consumer_up.set(0)


def build_redis_client(settings: Settings) -> Any:
    from redis import Redis

    return Redis.from_url(settings.redis_url, decode_responses=False)
