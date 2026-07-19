"""Two-stage streaming model services (#128).

Both draft and final models are supervised in child processes by default so a
native/GPU hang can be terminated and respawned without retaining an abandoned
model lock or unbounded audio snapshots.

Defaults follow ADR-0031: draft = `medium` int8, final = `large-v3-turbo`
(fp16). Both are intended for the GPU host; the module loads nothing at import
time, so CPU/CI environments are unaffected unless `/ws/stream` is used.
"""

from __future__ import annotations

import logging
import math
import multiprocessing as mp
import queue
import threading
import time
import uuid
from typing import Any, cast

import numpy as np

from app.core.config import Settings
from app.services.hallucination import is_hallucination
from app.services.worker import WorkerCrashedError, WorkerTimeoutError

logger = logging.getLogger(__name__)

# Defaults for direct construction (tests). Production callers pass the
# operator-tunable values from Settings via get_live_service/get_final_service,
# so the live path no longer drifts from the sync /transcribe worker path when
# STT_NO_SPEECH_THRESHOLD / STT_LOG_PROB_THRESHOLD / STT_COMPRESSION_RATIO_THRESHOLD
# are changed (#237). The values below equal the Settings defaults, so behavior
# is unchanged at the default configuration.
_DEFAULT_NO_SPEECH_THRESHOLD = 0.75
_DEFAULT_LOG_PROB_THRESHOLD = -1.0
_DEFAULT_COMPRESSION_RATIO_THRESHOLD = 2.4


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, float | int):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _usable_stream_segment(
    segment: object, no_speech_threshold: float, log_prob_threshold: float
) -> bool:
    text = str(getattr(segment, "text", "")).strip()
    if is_hallucination(text):
        return False

    no_speech_prob = _finite_float(getattr(segment, "no_speech_prob", None))
    if no_speech_prob is not None and no_speech_prob > no_speech_threshold:
        return False

    avg_logprob = _finite_float(getattr(segment, "avg_logprob", None))
    return avg_logprob is None or avg_logprob >= log_prob_threshold


class DirectWhisperService:
    """Lazy-loaded, lock-guarded faster-whisper wrapper for streaming."""

    def __init__(
        self,
        model_name: str,
        device: str,
        compute_type: str,
        language: str,
        beam_size: int,
        role: str = "stream",
        *,
        no_speech_threshold: float = _DEFAULT_NO_SPEECH_THRESHOLD,
        log_prob_threshold: float = _DEFAULT_LOG_PROB_THRESHOLD,
        compression_ratio_threshold: float = _DEFAULT_COMPRESSION_RATIO_THRESHOLD,
        condition_on_previous_text: bool = False,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.role = role
        self.no_speech_threshold = no_speech_threshold
        self.log_prob_threshold = log_prob_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.condition_on_previous_text = condition_on_previous_text
        self._model: object | None = None
        self._lock = threading.Lock()

    def ensure_model(self) -> None:
        """Load the model now (first call pays download/VRAM cost)."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    logger.info(
                        "Loading streaming Whisper model",
                        extra={
                            "model": self.model_name,
                            "device": self.device,
                            "compute_type": self.compute_type,
                            "beam_size": self.beam_size,
                        },
                    )
                    self._model = WhisperModel(
                        self.model_name,
                        device=self.device,
                        compute_type=self.compute_type,
                    )

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def transcribe_array(
        self, audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]], vad: bool
    ) -> str:
        """Transcribe a float32 PCM buffer; returns joined text.

        Decode thresholds come from Settings (#237) so the live stream path
        stays aligned with the sync /transcribe worker path when an operator
        tunes them; the defaults preserve the GPU demo tuning (no cross-window
        prompt conditioning over the rolling buffer, aggressive no-speech
        suppression).
        """
        self.ensure_model()
        assert self._model is not None
        with self._lock:
            segments, _info = self._model.transcribe(  # type: ignore[attr-defined]
                audio,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=vad,
                condition_on_previous_text=self.condition_on_previous_text,
                no_speech_threshold=self.no_speech_threshold,
                log_prob_threshold=self.log_prob_threshold,
                compression_ratio_threshold=self.compression_ratio_threshold,
            )
            return " ".join(
                str(segment.text).strip()
                for segment in segments
                if _usable_stream_segment(
                    segment, self.no_speech_threshold, self.log_prob_threshold
                )
            ).strip()


def _supervised_worker_main(config: dict[str, object], task_queue: Any, result_queue: Any) -> None:
    service = DirectWhisperService(
        cast(str, config["model_name"]),
        cast(str, config["device"]),
        cast(str, config["compute_type"]),
        cast(str, config["language"]),
        cast(int, config["beam_size"]),
        role=cast(str, config["role"]),
        no_speech_threshold=cast(float, config["no_speech_threshold"]),
        log_prob_threshold=cast(float, config["log_prob_threshold"]),
        compression_ratio_threshold=cast(float, config["compression_ratio_threshold"]),
        condition_on_previous_text=cast(bool, config["condition_on_previous_text"]),
    )
    while True:
        task = task_queue.get()
        if task.get("type") == "stop":
            return
        job_id = str(task.get("job_id", ""))
        try:
            if task.get("type") == "load":
                service.ensure_model()
                text = ""
            else:
                raw = task.get("audio")
                if not isinstance(raw, bytes):
                    raise ValueError("streaming audio payload is invalid")
                audio = np.frombuffer(raw, dtype="<f4").copy()
                text = service.transcribe_array(audio, bool(task.get("vad", False)))
            result_queue.put({"job_id": job_id, "ok": True, "text": text})
        except BaseException as exc:  # noqa: BLE001 - child reports class only
            result_queue.put({"job_id": job_id, "ok": False, "error_class": type(exc).__name__})


class _SupervisedWhisperService:
    """Single-flight model with terminate/kill/respawn timeout semantics."""

    hard_timeout = True

    def __init__(self, settings: Settings, *, role: str) -> None:
        self.role = role
        is_final = role == "final"
        self._config: dict[str, object] = {
            "role": role,
            "model_name": settings.final_model_name if is_final else settings.live_model_name,
            "device": settings.final_device if is_final else settings.live_device,
            "compute_type": (
                settings.final_compute_type if is_final else settings.live_compute_type
            ),
            "language": settings.language,
            "beam_size": settings.final_beam_size if is_final else settings.live_beam_size,
            "no_speech_threshold": settings.no_speech_threshold,
            "log_prob_threshold": settings.log_prob_threshold,
            "compression_ratio_threshold": settings.compression_ratio_threshold,
            "condition_on_previous_text": settings.condition_on_previous_text,
        }
        self._timeout_sec = (
            settings.stream_final_timeout_sec if is_final else settings.stream_live_timeout_sec
        )
        self._load_timeout_sec = settings.stream_model_load_timeout_sec
        self._kill_grace_sec = settings.worker_kill_grace_sec
        self._ctx = mp.get_context("spawn")
        self._call_lock = threading.Lock()
        self._process: Any | None = None
        self._task_queue: Any = None
        self._result_queue: Any = None
        self._model_loaded = False
        self._restart_blocked = False
        self._generation = 0
        self._start()

    def _start(self) -> None:
        self._generation = getattr(self, "_generation", 0) + 1
        self._task_queue = self._ctx.Queue(maxsize=1)
        self._result_queue = self._ctx.Queue(maxsize=1)
        self._process = self._ctx.Process(
            target=_supervised_worker_main,
            args=(self._config, self._task_queue, self._result_queue),
            name=f"stt-stream-{self.role}-worker",
            daemon=True,
        )
        self._process.start()
        self._model_loaded = False
        self._restart_blocked = False

    def _is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @staticmethod
    def _close_queue(queue_object: Any) -> None:
        close = getattr(queue_object, "close", None)
        if callable(close):
            close()
        cancel_join_thread = getattr(queue_object, "cancel_join_thread", None)
        if callable(cancel_join_thread):
            cancel_join_thread()

    def _terminate_and_restart(self, *, restart: bool = True) -> None:
        task_queue = self._task_queue
        result_queue = self._result_queue
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=self._kill_grace_sec)
            if process.is_alive():
                process.kill()
                process.join(timeout=self._kill_grace_sec)
            if process.is_alive():
                # Starting another GPU worker while the old native process is
                # still alive would make process/VRAM use unbounded. Keep the
                # supervisor permanently fail-closed until the service is
                # restarted by its orchestrator.
                self._model_loaded = False
                self._restart_blocked = True
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker could not be stopped safely"
                )
        self._close_queue(task_queue)
        self._close_queue(result_queue)
        self._process = None
        self._model_loaded = False
        if restart:
            self._start()

    def _invoke(
        self,
        operation: str,
        *,
        timeout_sec: float,
        audio: bytes | None = None,
        vad: bool = False,
        restart_on_failure: bool = True,
        required_generation: int | None = None,
        require_loaded: bool = False,
    ) -> str:
        deadline = time.monotonic() + timeout_sec
        if getattr(self, "_restart_blocked", False):
            raise WorkerCrashedError(
                f"streaming {getattr(self, 'role', 'final')} worker restart is blocked"
            )
        if not self._call_lock.acquire(timeout=timeout_sec):
            raise WorkerTimeoutError(
                f"streaming {getattr(self, 'role', 'final')} worker queue exceeded timeout"
            )
        try:
            if required_generation is not None and (
                self._generation != required_generation
                or (require_loaded and not self._model_loaded)
                or not self._is_alive()
            ):
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker readiness changed"
                )
            if not self._is_alive():
                if not restart_on_failure:
                    raise WorkerCrashedError(
                        f"streaming {getattr(self, 'role', 'final')} worker is not alive"
                    )
                self._terminate_and_restart()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerTimeoutError(
                    f"streaming {getattr(self, 'role', 'final')} worker queue exceeded timeout"
                )
            job_id = str(uuid.uuid4())
            try:
                self._task_queue.put(
                    {
                        "type": operation,
                        "job_id": job_id,
                        "audio": audio,
                        "vad": vad,
                    },
                    timeout=remaining,
                )
            except queue.Full as exc:
                self._terminate_and_restart(restart=restart_on_failure)
                raise WorkerTimeoutError(
                    f"streaming {getattr(self, 'role', 'final')} worker queue exceeded timeout"
                ) from exc
            while True:
                if not self._is_alive():
                    self._terminate_and_restart(restart=restart_on_failure)
                    raise WorkerCrashedError(
                        f"streaming {getattr(self, 'role', 'final')} worker exited before response"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_and_restart(restart=restart_on_failure)
                    raise WorkerTimeoutError(
                        f"streaming {getattr(self, 'role', 'final')} worker exceeded timeout"
                    )
                try:
                    response = self._result_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if response.get("job_id") != job_id:
                    continue
                if not response.get("ok"):
                    raise RuntimeError(str(response.get("error_class", "RuntimeError")))
                text = str(response.get("text", ""))
                if operation == "load":
                    self._model_loaded = True
                return text
        finally:
            self._call_lock.release()

    def ensure_model(self) -> None:
        if not self._model_loaded:
            self._invoke("load", timeout_sec=self._load_timeout_sec)

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded and self._is_alive()

    @property
    def ready_generation(self) -> int:
        """Return the loaded worker generation under the single-flight lock."""
        with self._call_lock:
            if not self._model_loaded or not self._is_alive():
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker is not ready"
                )
            return self._generation

    def transcribe_array(
        self, audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]], vad: bool
    ) -> str:
        # A timeout recycles the child process. Reload the pinned model under the
        # separate cold-load budget before applying the much shorter inference
        # budget; otherwise the first post-timeout request can repeatedly kill a
        # healthy replacement while it is still loading the model.
        self.ensure_model()
        contiguous = np.ascontiguousarray(audio, dtype=np.float32)
        return self._invoke(
            "transcribe",
            timeout_sec=self._timeout_sec,
            audio=contiguous.astype("<f4", copy=False).tobytes(),
            vad=vad,
        )

    def transcribe_loaded_array(
        self,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
        expected_generation: int,
    ) -> str:
        """Decode only with the worker that was proven ready for this stream.

        EOF must never hide a cold model reload behind the terminal timeout
        advertised during the ready handshake. If the supervised worker was
        recycled after readiness, this connection fails closed and a new
        connection performs model loading before it can receive audio.
        """
        contiguous = np.ascontiguousarray(audio, dtype=np.float32)
        return self._invoke(
            "transcribe",
            timeout_sec=self._timeout_sec,
            audio=contiguous.astype("<f4", copy=False).tobytes(),
            vad=vad,
            restart_on_failure=False,
            required_generation=expected_generation,
            require_loaded=True,
        )

    @property
    def healthy(self) -> bool:
        return not self._restart_blocked and self._is_alive()

    def close(self) -> None:
        with self._call_lock:
            self._terminate_and_restart(restart=False)


class SupervisedFinalWhisperService(_SupervisedWhisperService):
    role = "final"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, role="final")


class SupervisedLiveWhisperService(_SupervisedWhisperService):
    role = "live"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, role="live")


_services: dict[str, DirectWhisperService] = {}
_supervised_live_services: dict[str, SupervisedLiveWhisperService] = {}
_supervised_final_services: dict[str, SupervisedFinalWhisperService] = {}
_services_lock = threading.Lock()


def _named(
    key: str,
    model_name: str,
    device: str,
    compute_type: str,
    language: str,
    beam_size: int,
    *,
    no_speech_threshold: float,
    log_prob_threshold: float,
    compression_ratio_threshold: float,
    condition_on_previous_text: bool,
) -> DirectWhisperService:
    # Decode thresholds are part of the cache identity so a settings change
    # yields a fresh service rather than a stale cached one (#237).
    service_key = "\u0000".join(
        [
            key,
            model_name,
            device,
            compute_type,
            language,
            str(beam_size),
            str(no_speech_threshold),
            str(log_prob_threshold),
            str(compression_ratio_threshold),
            str(condition_on_previous_text),
        ]
    )
    with _services_lock:
        if service_key not in _services:
            _services[service_key] = DirectWhisperService(
                model_name,
                device,
                compute_type,
                language,
                beam_size,
                role=key,
                no_speech_threshold=no_speech_threshold,
                log_prob_threshold=log_prob_threshold,
                compression_ratio_threshold=compression_ratio_threshold,
                condition_on_previous_text=condition_on_previous_text,
            )
        return _services[service_key]


def get_live_service(
    settings: Settings,
) -> DirectWhisperService | SupervisedLiveWhisperService:
    """Fast draft model (ADR-0031: medium int8)."""
    if settings.stream_live_worker_backend == "process":
        service_key = "\u0000".join(
            [
                settings.live_model_name,
                settings.live_device,
                settings.live_compute_type,
                settings.language,
                str(settings.live_beam_size),
                str(settings.stream_live_timeout_sec),
                str(settings.stream_model_load_timeout_sec),
            ]
        )
        with _services_lock:
            if service_key not in _supervised_live_services:
                _supervised_live_services[service_key] = SupervisedLiveWhisperService(settings)
            return _supervised_live_services[service_key]
    return _named(
        "live",
        settings.live_model_name,
        settings.live_device,
        settings.live_compute_type,
        settings.language,
        settings.live_beam_size,
        no_speech_threshold=settings.no_speech_threshold,
        log_prob_threshold=settings.log_prob_threshold,
        compression_ratio_threshold=settings.compression_ratio_threshold,
        condition_on_previous_text=settings.condition_on_previous_text,
    )


def get_final_service(
    settings: Settings,
) -> DirectWhisperService | SupervisedFinalWhisperService:
    """Accurate final model (ADR-0031: large-v3-turbo)."""
    if settings.stream_final_worker_backend == "process":
        service_key = "\u0000".join(
            [
                settings.final_model_name,
                settings.final_device,
                settings.final_compute_type,
                settings.language,
                str(settings.final_beam_size),
                str(settings.stream_final_timeout_sec),
                str(settings.stream_model_load_timeout_sec),
            ]
        )
        with _services_lock:
            if service_key not in _supervised_final_services:
                _supervised_final_services[service_key] = SupervisedFinalWhisperService(settings)
            return _supervised_final_services[service_key]
    return _named(
        "final",
        settings.final_model_name,
        settings.final_device,
        settings.final_compute_type,
        settings.language,
        settings.final_beam_size,
        no_speech_threshold=settings.no_speech_threshold,
        log_prob_threshold=settings.log_prob_threshold,
        compression_ratio_threshold=settings.compression_ratio_threshold,
        condition_on_previous_text=settings.condition_on_previous_text,
    )


def streaming_services_healthy() -> bool:
    with _services_lock:
        supervised = [*_supervised_live_services.values(), *_supervised_final_services.values()]
        return all(service.healthy for service in supervised)


def shutdown_streaming_services() -> None:
    with _services_lock:
        supervised = [*_supervised_live_services.values(), *_supervised_final_services.values()]
        _supervised_live_services.clear()
        _supervised_final_services.clear()
    for service in supervised:
        service.close()
