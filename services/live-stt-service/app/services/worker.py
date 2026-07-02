"""Subprocess worker pool for Whisper inference.

PR-stt-03 / #20 scope:
- Whisper inference runs in `multiprocessing.Process` workers by default.
- Each worker is single-flight: one inference at a time.
- The parent supervises worker liveness and respawns a crashed worker.
- The model is lazy-loaded once inside each worker process.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing as mp
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO, Protocol

from app.core.config import Settings, resolve_worker_count
from app.models.schemas import TranscribeResponse

logger = logging.getLogger(__name__)


class WorkerCrashedError(RuntimeError):
    """Raised when a child process exits before returning a response."""


class WorkerTimeoutError(TimeoutError):
    """Raised after a timed-out worker is terminated/killed and respawned."""


class WorkerPool(Protocol):
    """Minimal interface used by TranscribeService."""

    def transcribe(
        self,
        audio: BinaryIO | str,
        timeout_sec: float | None = None,
        language: str | None = None,
    ) -> TranscribeResponse:
        """Run one transcription."""

    @property
    def model_loaded(self) -> bool:
        """Whether at least one worker has loaded the model."""


@dataclass(frozen=True)
class _WorkerConfig:
    model_name: str
    compute_type: str
    device: str
    language: str
    beam_size: int
    vad_filter: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> _WorkerConfig:
        return cls(
            model_name=settings.model_name,
            compute_type=settings.compute_type,
            device=settings.device,
            language=settings.language,
            beam_size=settings.beam_size,
            vad_filter=settings.vad_filter,
        )


def _audio_payload(audio: BinaryIO | str) -> dict[str, bytes | str | None]:
    if isinstance(audio, str):
        return {"path": audio, "bytes": None}
    return {"path": None, "bytes": audio.read()}


def _audio_from_payload(payload: dict[str, bytes | str | None]) -> BytesIO | str:
    path = payload.get("path")
    if isinstance(path, str):
        return path
    raw = payload.get("bytes")
    if not isinstance(raw, bytes):
        raw = b""
    return BytesIO(raw)


def _transcribe_with_model(
    model: object,
    cfg: _WorkerConfig,
    audio_payload: dict[str, bytes | str | None],
    language_override: str | None = None,
) -> dict[str, Any]:
    requested_language = language_override or cfg.language
    language = None if requested_language == "auto" else requested_language
    start = time.perf_counter()
    segments_iter, info = model.transcribe(  # type: ignore[attr-defined]
        _audio_from_payload(audio_payload),
        language=language,
        beam_size=cfg.beam_size,
        vad_filter=cfg.vad_filter,
    )
    segments = [
        {
            "id": idx,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "avg_logprob": getattr(seg, "avg_logprob", None),
            "no_speech_prob": getattr(seg, "no_speech_prob", None),
        }
        for idx, seg in enumerate(segments_iter)
    ]
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    full_text = " ".join(str(s["text"]) for s in segments).strip()
    return {
        "text": full_text,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "elapsed_ms": elapsed_ms,
        "model": cfg.model_name,
        "compute_type": cfg.compute_type,
        "device": cfg.device,
        "segments": segments,
    }


def _raise_worker_error(error_class: str) -> None:
    if error_class == "MemoryError":
        raise MemoryError(error_class)
    if error_class == "OSError":
        raise OSError(error_class)
    if error_class == "ValueError":
        raise ValueError(error_class)
    raise RuntimeError(error_class)


def _worker_main(
    cfg: _WorkerConfig,
    task_queue: Any,
    result_queue: Any,
) -> None:
    model: object | None = None
    while True:
        task = task_queue.get()
        if task.get("type") == "stop":
            return

        job_id = str(task["job_id"])
        try:
            if model is None:
                from faster_whisper import WhisperModel

                logger.info(
                    "Loading Whisper model in worker",
                    extra={
                        "model": cfg.model_name,
                        "device": cfg.device,
                        "compute_type": cfg.compute_type,
                    },
                )
                model = WhisperModel(
                    cfg.model_name,
                    device=cfg.device,
                    compute_type=cfg.compute_type,
                )
            payload = task["audio"]
            assert isinstance(payload, dict)
            language = task.get("language")
            result_queue.put(
                {
                    "job_id": job_id,
                    "ok": True,
                    "result": _transcribe_with_model(
                        model,
                        cfg,
                        payload,
                        language if isinstance(language, str) else None,
                    ),
                }
            )
        except BaseException as exc:  # noqa: BLE001 - child must report sanitized class
            result_queue.put(
                {
                    "job_id": job_id,
                    "ok": False,
                    "error_class": type(exc).__name__,
                }
            )


class InlineWorkerPool:
    """In-process backend used by tests; production default is process."""

    def __init__(self, settings: Settings) -> None:
        self._cfg = _WorkerConfig.from_settings(settings)
        self._model: object | None = None

    def _ensure_model(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._cfg.model_name,
                device=self._cfg.device,
                compute_type=self._cfg.compute_type,
            )
        return self._model

    def transcribe(
        self,
        audio: BinaryIO | str,
        timeout_sec: float | None = None,
        language: str | None = None,
    ) -> TranscribeResponse:
        model = self._ensure_model()
        payload = _audio_payload(audio)
        return TranscribeResponse(**_transcribe_with_model(model, self._cfg, payload, language))

    @property
    def model_loaded(self) -> bool:
        return self._model is not None


class _WorkerSlot:
    def __init__(self, cfg: _WorkerConfig, index: int) -> None:
        self.cfg = cfg
        self.index = index
        self.ctx = mp.get_context("spawn")
        self.task_queue: Any = self.ctx.Queue(maxsize=1)
        self.result_queue: Any = self.ctx.Queue(maxsize=1)
        self.process: Any | None = None
        self.model_loaded = False
        self.start()

    def start(self) -> None:
        self.process = self.ctx.Process(
            target=_worker_main,
            args=(self.cfg, self.task_queue, self.result_queue),
            name=f"stt-worker-{self.index}",
            daemon=True,
        )
        self.process.start()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.is_alive()

    def restart(self) -> None:
        self.stop()
        self.task_queue = self.ctx.Queue(maxsize=1)
        self.result_queue = self.ctx.Queue(maxsize=1)
        self.model_loaded = False
        self.start()

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.is_alive():
            try:
                self.task_queue.put_nowait({"type": "stop"})
                self.process.join(timeout=2)
            except Exception as exc:  # noqa: BLE001 - best effort shutdown
                logger.debug(
                    "Worker graceful stop failed",
                    extra={"err_class": type(exc).__name__},
                )
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
        self.process = None

    def kill_for_timeout(self, grace_sec: float) -> None:
        if self.process is not None and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=grace_sec)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=grace_sec)
        self.process = None
        self.task_queue = self.ctx.Queue(maxsize=1)
        self.result_queue = self.ctx.Queue(maxsize=1)
        self.model_loaded = False
        self.start()


class ProcessWorkerPool:
    """Small supervised process pool with single-flight workers."""

    def __init__(self, settings: Settings) -> None:
        self._cfg = _WorkerConfig.from_settings(settings)
        self._kill_grace_sec = settings.worker_kill_grace_sec
        # #42: optional GPU VRAM admission. Disabled unless device=cuda and a
        # positive STT_WORKER_VRAM_BUDGET_MB is set, so default/CPU behaviour is
        # unchanged. Prevents the measured K=4 all-fail collapse on 8 GiB.
        plan = resolve_worker_count(settings)
        if plan.clamped:
            logger.warning(
                "Clamping STT worker count to fit GPU VRAM budget",
                extra={
                    "requested": plan.requested,
                    "effective": plan.effective,
                    "affordable": plan.affordable,
                    "vram_budget_mb": settings.worker_vram_budget_mb,
                    "vram_per_worker_mb": settings.worker_vram_per_worker_mb,
                },
            )
        self._slots = [_WorkerSlot(self._cfg, index=i) for i in range(plan.effective)]
        self._available = threading.BoundedSemaphore(value=len(self._slots))
        self._lock = threading.Lock()
        self._next_slot = 0
        self._busy: set[int] = set()

    def _acquire_slot(self) -> _WorkerSlot:
        self._available.acquire()
        with self._lock:
            for offset in range(len(self._slots)):
                idx = (self._next_slot + offset) % len(self._slots)
                if idx not in self._busy:
                    self._busy.add(idx)
                    self._next_slot = (idx + 1) % len(self._slots)
                    slot = self._slots[idx]
                    if not slot.is_alive():
                        logger.warning("Respawning crashed STT worker", extra={"worker": idx})
                        slot.restart()
                    return slot
        self._available.release()
        raise RuntimeError("No STT worker slot available")

    def _release_slot(self, slot: _WorkerSlot) -> None:
        with self._lock:
            self._busy.discard(slot.index)
        self._available.release()

    def transcribe(
        self,
        audio: BinaryIO | str,
        timeout_sec: float | None = None,
        language: str | None = None,
    ) -> TranscribeResponse:
        slot = self._acquire_slot()
        try:
            job_id = str(uuid.uuid4())
            deadline = time.monotonic() + timeout_sec if timeout_sec is not None else None
            slot.task_queue.put(
                {
                    "type": "transcribe",
                    "job_id": job_id,
                    "audio": _audio_payload(audio),
                    "language": language,
                }
            )
            while True:
                if not slot.is_alive():
                    slot.restart()
                    raise WorkerCrashedError("STT worker exited before response")
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        slot.kill_for_timeout(self._kill_grace_sec)
                        raise WorkerTimeoutError("STT worker exceeded timeout")
                    wait_sec = min(0.2, remaining)
                else:
                    wait_sec = 0.2
                try:
                    response = slot.result_queue.get(timeout=wait_sec)
                except queue.Empty:
                    continue
                if response.get("job_id") != job_id:
                    continue
                if not response.get("ok"):
                    _raise_worker_error(str(response.get("error_class", "RuntimeError")))
                result = response["result"]
                assert isinstance(result, dict)
                slot.model_loaded = True
                return TranscribeResponse(**result)
        finally:
            self._release_slot(slot)

    @property
    def model_loaded(self) -> bool:
        return any(slot.model_loaded for slot in self._slots)

    def close(self) -> None:
        for slot in self._slots:
            slot.stop()


# ---------------------------------------------------------------------------
# #42 shared-model multi-stream backend
#
# One supervised subprocess hosts a SINGLE WhisperModel(num_workers=K). Weights
# are loaded once, so VRAM stays ~flat as concurrency grows (fixes the measured
# linear 2->4->6 GiB / K=4 OOM of the per-worker process pool). Concurrency is
# K simultaneous model.transcribe() calls dispatched over a ThreadPoolExecutor;
# CTranslate2 assigns each call to its own worker / CUDA stream.
#
# #20-22 hard-kill guarantee is preserved at *supervisor* granularity: a timed
# out job triggers terminate -> grace -> kill of the whole supervisor, which is
# then respawned. The trade-off (documented for #42) is that the kill also
# fails the other in-flight jobs on that supervisor — they surface as a
# controlled WorkerCrashedError rather than a per-request SIGKILL.
# ---------------------------------------------------------------------------

_CRASH_SENTINEL = "__supervisor_crashed__"


def _supervisor_main(
    cfg: _WorkerConfig,
    num_streams: int,
    task_queue: Any,
    result_queue: Any,
) -> None:
    model: object | None = None
    executor: ThreadPoolExecutor | None = None

    def _run_job(
        job_id: str,
        payload: dict[str, bytes | str | None],
        language: str | None,
    ) -> None:
        assert model is not None
        try:
            result = _transcribe_with_model(model, cfg, payload, language)
            result_queue.put({"job_id": job_id, "ok": True, "result": result})
        except BaseException as exc:  # noqa: BLE001 - report sanitized class only
            result_queue.put({"job_id": job_id, "ok": False, "error_class": type(exc).__name__})

    while True:
        task = task_queue.get()
        if task.get("type") == "stop":
            if executor is not None:
                executor.shutdown(wait=False)
            return
        if model is None:
            # Local import keeps faster-whisper out of the default/test import path.
            from faster_whisper import WhisperModel

            logger.info(
                "Loading shared Whisper model",
                extra={
                    "model": cfg.model_name,
                    "device": cfg.device,
                    "compute_type": cfg.compute_type,
                    "num_streams": num_streams,
                },
            )
            # num_workers=K -> CTranslate2 runs K concurrent inference workers,
            # each on its own CUDA stream, over the SHARED model weights.
            model = WhisperModel(
                cfg.model_name,
                device=cfg.device,
                compute_type=cfg.compute_type,
                num_workers=num_streams,
            )
            executor = ThreadPoolExecutor(max_workers=num_streams)
        assert executor is not None
        payload = task["audio"]
        assert isinstance(payload, dict)
        language = task.get("language")
        executor.submit(
            _run_job,
            str(task["job_id"]),
            payload,
            language if isinstance(language, str) else None,
        )


class SharedModelWorkerPool:
    """Single supervised process hosting one shared model over K CUDA streams."""

    def __init__(self, settings: Settings) -> None:
        self._cfg = _WorkerConfig.from_settings(settings)
        self._kill_grace_sec = settings.worker_kill_grace_sec
        # #42: per-stream VRAM admission. In shared mode the model weights are
        # loaded ONCE, so worker_vram_per_worker_mb is the marginal per-stream
        # budget (operator-measured on the target GPU), not a full model copy.
        plan = resolve_worker_count(settings)
        if plan.clamped:
            logger.warning(
                "Clamping STT CUDA stream count to fit GPU VRAM budget",
                extra={
                    "requested": plan.requested,
                    "effective": plan.effective,
                    "affordable": plan.affordable,
                    "vram_budget_mb": settings.worker_vram_budget_mb,
                    "vram_per_worker_mb": settings.worker_vram_per_worker_mb,
                },
            )
        self._num_streams = plan.effective
        self._ctx = mp.get_context("spawn")
        self._lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._available = threading.BoundedSemaphore(self._num_streams)
        self._model_loaded = False
        self._generation = 0
        self._process: Any | None = None
        self._task_queue: Any = None
        self._result_queue: Any = None
        self._demux_stop: threading.Event | None = None
        self._start()

    def _start(self) -> None:
        self._task_queue = self._ctx.Queue()
        self._result_queue = self._ctx.Queue()
        self._generation += 1
        self._process = self._ctx.Process(
            target=_supervisor_main,
            args=(self._cfg, self._num_streams, self._task_queue, self._result_queue),
            name="stt-shared-supervisor",
            daemon=True,
        )
        self._process.start()
        stop_event = threading.Event()
        self._demux_stop = stop_event
        result_queue = self._result_queue
        demux = threading.Thread(
            target=self._demux_loop,
            args=(result_queue, stop_event),
            name="stt-shared-demux",
            daemon=True,
        )
        demux.start()

    def _demux_loop(self, result_queue: Any, stop_event: threading.Event) -> None:
        """Fan results from the single shared result queue to per-job queues."""
        while not stop_event.is_set():
            try:
                resp = result_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                jq = self._pending.get(str(resp.get("job_id")))
            if jq is not None:
                with contextlib.suppress(queue.Full):
                    jq.put_nowait(resp)

    def _kill_process(self, proc: Any | None) -> None:
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=self._kill_grace_sec)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=self._kill_grace_sec)

    def _restart_after_timeout(self, generation: int) -> None:
        with self._restart_lock:
            if generation != self._generation:
                return  # another thread already recycled this supervisor
            if self._demux_stop is not None:
                self._demux_stop.set()
            self._kill_process(self._process)
            with self._lock:
                pending = list(self._pending.values())
            for jq in pending:  # collateral: fail siblings with a crash sentinel
                with contextlib.suppress(queue.Full):
                    jq.put_nowait({"ok": False, "error_class": _CRASH_SENTINEL})
            self._start()

    def transcribe(
        self,
        audio: BinaryIO | str,
        timeout_sec: float | None = None,
        language: str | None = None,
    ) -> TranscribeResponse:
        self._available.acquire()
        job_id = str(uuid.uuid4())
        jq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[job_id] = jq
            task_queue = self._task_queue
            process = self._process
            generation = self._generation
        try:
            if process is None or not process.is_alive():
                raise WorkerCrashedError("STT shared supervisor not running")
            task_queue.put(
                {
                    "type": "transcribe",
                    "job_id": job_id,
                    "audio": _audio_payload(audio),
                    "language": language,
                }
            )
            try:
                response = jq.get(timeout=timeout_sec) if timeout_sec is not None else jq.get()
            except queue.Empty:
                self._restart_after_timeout(generation)
                raise WorkerTimeoutError("STT shared supervisor exceeded timeout") from None
            error_class = str(response.get("error_class", "RuntimeError"))
            if not response.get("ok"):
                if error_class == _CRASH_SENTINEL:
                    raise WorkerCrashedError("STT shared supervisor recycled mid-flight")
                _raise_worker_error(error_class)
            result = response["result"]
            assert isinstance(result, dict)
            self._model_loaded = True
            return TranscribeResponse(**result)
        finally:
            with self._lock:
                self._pending.pop(job_id, None)
            self._available.release()

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    def close(self) -> None:
        with self._restart_lock:
            if self._demux_stop is not None:
                self._demux_stop.set()
            if self._task_queue is not None:
                try:
                    self._task_queue.put_nowait({"type": "stop"})
                except Exception as exc:  # noqa: BLE001 - best effort shutdown
                    logger.debug(
                        "Shared supervisor graceful stop failed",
                        extra={"err_class": type(exc).__name__},
                    )
            if self._process is not None:
                self._process.join(timeout=2)
                if self._process.is_alive():
                    self._kill_process(self._process)


def build_worker_pool(settings: Settings) -> WorkerPool:
    if settings.worker_backend == "inline":
        return InlineWorkerPool(settings)
    if settings.worker_backend == "shared":
        return SharedModelWorkerPool(settings)
    return ProcessWorkerPool(settings)
