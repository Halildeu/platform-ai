"""Two-stage streaming model services (#128).

Both draft and final models are supervised in child processes by default so a
native/GPU hang can be terminated and respawned without retaining an abandoned
model lock or unbounded audio snapshots.

Defaults follow ADR-0031: draft = `medium` int8, final = `large-v3-turbo`
(fp16). Both are intended for the GPU host; the module loads nothing at import
time, so CPU/CI environments are unaffected unless `/ws/stream` is used.
"""

from __future__ import annotations

import hashlib
import logging
import math
import multiprocessing as mp
import os
import queue
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

import numpy as np

from app.core.config import Settings
from app.services.hallucination import (
    is_contextual_silence_hallucination,
    is_hallucination,
)
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
_MODEL_DIRECTORY_HASH_DOMAIN = b"platform-live-stt-model-directory-v1\0"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_or_reparse(file_stat: os.stat_result) -> bool:
    """Detect Unix links and Windows reparse points without following them."""
    attributes = int(getattr(file_stat, "st_file_attributes", 0))
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _stable_stat_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _assert_path_components_are_directories(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        current_stat = current.lstat()
        if _is_link_or_reparse(current_stat):
            raise ValueError("pinned streaming model path contains a link or reparse point")
        if current != path and not stat.S_ISDIR(current_stat.st_mode):
            raise ValueError("pinned streaming model path contains a non-directory component")


def _regular_model_files(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    files: list[tuple[str, Path, os.stat_result]] = []

    def visit(directory: Path, relative_directory: Path) -> None:
        before = directory.lstat()
        if _is_link_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
            raise ValueError("pinned streaming model directory contains a link or reparse point")
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            entry_path = Path(entry.path)
            entry_stat = entry.stat(follow_symlinks=False)
            if _is_link_or_reparse(entry_stat):
                raise ValueError(
                    "pinned streaming model directory contains a link or reparse point"
                )
            relative_path = relative_directory / entry.name
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(entry_path, relative_path)
            elif stat.S_ISREG(entry_stat.st_mode):
                # Record a PATH-derived stat, never the scandir entry's.
                #
                # The recorded stat is later compared against os.fstat() of the
                # opened descriptor to prove the file was not swapped between
                # listing and reading. On Windows the directory-enumeration API
                # behind os.scandir() does not carry the volume serial or file
                # index, so entry.stat() reports st_dev=0 and st_ino=0 while
                # fstat() reports the real values — measured on the GPU host:
                #
                #   field      scandir  fstat
                #   st_dev     0        2659991151
                #   st_ino     0        2251799815012715
                #
                # That made the identity comparison unequal for every file, so
                # verification failed on Windows always and the hardened model
                # runtime could never load. os.lstat() on the same path does
                # populate both fields (measured equal to fstat), so re-statting
                # keeps the anti-swap guard exact instead of weakening it.
                #
                # Invisible on Linux, where scandir carries d_ino.
                listed_stat = entry_path.lstat()
                if _is_link_or_reparse(listed_stat) or not stat.S_ISREG(listed_stat.st_mode):
                    raise ValueError(
                        "pinned streaming model directory contains a link or reparse point"
                    )
                files.append((relative_path.as_posix(), entry_path, listed_stat))
            else:
                raise ValueError("pinned streaming model directory contains a non-regular file")
        after = directory.lstat()
        if _is_link_or_reparse(after) or _stable_stat_identity(before) != _stable_stat_identity(
            after
        ):
            raise ValueError("pinned streaming model directory mutated during verification")

    visit(root, Path())
    return sorted(files, key=lambda item: item[0])


def _stream_model_directory_sha256(model_path: str | Path) -> str:
    """Hash a stable manifest of every regular file under a link-free model root."""
    root = Path(os.path.abspath(os.fspath(model_path)))
    _assert_path_components_are_directories(root)
    if not root.is_dir():
        raise FileNotFoundError("pinned streaming model directory does not exist")

    digest = hashlib.sha256(_MODEL_DIRECTORY_HASH_DOMAIN)
    for relative_path, file_path, listed_stat in _regular_model_files(root):
        encoded_path = relative_path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(listed_stat.st_size.to_bytes(8, "big"))

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(file_path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if _is_link_or_reparse(opened_stat) or not stat.S_ISREG(opened_stat.st_mode):
                raise ValueError("pinned streaming model manifest member is not a regular file")
            if _stable_stat_identity(listed_stat) != _stable_stat_identity(opened_stat):
                raise ValueError("pinned streaming model file mutated during verification")
            with os.fdopen(descriptor, "rb", closefd=False) as model_file:
                for block in iter(lambda: model_file.read(1024 * 1024), b""):
                    digest.update(block)
            after_read = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        path_after_read = file_path.lstat()
        expected_identity = _stable_stat_identity(opened_stat)
        if (
            _is_link_or_reparse(path_after_read)
            or expected_identity != _stable_stat_identity(after_read)
            or expected_identity != _stable_stat_identity(path_after_read)
        ):
            raise ValueError("pinned streaming model file mutated during verification")
    return digest.hexdigest()


def _verify_stream_model_directory(
    model_path: str | Path,
    expected_model_sha256: str,
    expected_tree_sha256: str,
) -> Path:
    root = Path(os.path.abspath(os.fspath(model_path)))
    tree_digest = _stream_model_directory_sha256(root)
    model_bin = root / "model.bin"
    if not model_bin.is_file():
        raise FileNotFoundError("pinned streaming model directory does not contain model.bin")
    expected_model = expected_model_sha256.removeprefix("sha256:").lower()
    if expected_model and _sha256_file(model_bin) != expected_model:
        raise ValueError("pinned streaming model SHA-256 mismatch")
    expected_tree = expected_tree_sha256.removeprefix("sha256:").lower()
    if expected_tree and tree_digest != expected_tree:
        raise ValueError("pinned streaming model tree SHA-256 mismatch")
    return root


def _sha256_file(path: Path) -> str:
    _assert_path_components_are_directories(path.parent)
    listed_stat = path.lstat()
    if _is_link_or_reparse(listed_stat) or not stat.S_ISREG(listed_stat.st_mode):
        raise ValueError("pinned streaming model manifest member is not a regular file")

    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if _stable_stat_identity(listed_stat) != _stable_stat_identity(opened_stat):
            raise ValueError("pinned streaming model file mutated during verification")
        with os.fdopen(descriptor, "rb", closefd=False) as model_file:
            for block in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(block)
        after_read = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    path_after_read = path.lstat()
    expected_identity = _stable_stat_identity(opened_stat)
    if (
        _is_link_or_reparse(path_after_read)
        or expected_identity != _stable_stat_identity(after_read)
        or expected_identity != _stable_stat_identity(path_after_read)
    ):
        raise ValueError("pinned streaming model file mutated during verification")
    return digest.hexdigest()


def _model_tree_sha256(root: Path) -> str:
    """Backward-compatible name for the full, link-safe directory manifest hash."""
    return _stream_model_directory_sha256(root)


def _resolve_stream_model_source(
    model_name: str,
    model_path: str | None,
    model_sha256: str,
    model_tree_sha256: str = "",
) -> str:
    """Resolve and verify the exact streaming model bytes before GPU allocation."""
    if model_path is None:
        return model_name
    return str(_verify_stream_model_directory(model_path, model_sha256, model_tree_sha256))


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, float | int):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _usable_stream_segment(
    segment: object,
    no_speech_threshold: float,
    log_prob_threshold: float,
    *,
    audio_rms: float | None = None,
) -> bool:
    text = str(getattr(segment, "text", "")).strip()
    if is_hallucination(text):
        return False

    no_speech_prob = _finite_float(getattr(segment, "no_speech_prob", None))
    if is_contextual_silence_hallucination(
        text,
        audio_rms=audio_rms,
        no_speech_prob=no_speech_prob,
    ):
        return False
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
        model_path: str | None = None,
        model_sha256: str = "",
        model_tree_sha256: str = "",
        no_speech_threshold: float = _DEFAULT_NO_SPEECH_THRESHOLD,
        log_prob_threshold: float = _DEFAULT_LOG_PROB_THRESHOLD,
        compression_ratio_threshold: float = _DEFAULT_COMPRESSION_RATIO_THRESHOLD,
        condition_on_previous_text: bool = False,
        vad_parameters: dict[str, float | int] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.role = role
        self.model_path = model_path
        self.model_sha256 = model_sha256
        self.model_tree_sha256 = model_tree_sha256
        self.no_speech_threshold = no_speech_threshold
        self.log_prob_threshold = log_prob_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.condition_on_previous_text = condition_on_previous_text
        self.vad_parameters = dict(vad_parameters or {})
        self._model: object | None = None
        self._lock = threading.Lock()

    def ensure_model(
        self,
        *,
        deadline: float | None = None,
        cleanup_deadline: float | None = None,
    ) -> None:
        """Load the model now (first call pays download/VRAM cost)."""
        del deadline, cleanup_deadline  # Inline backend is forbidden in staging/production.
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
                    model_source = _resolve_stream_model_source(
                        self.model_name,
                        self.model_path,
                        self.model_sha256,
                        self.model_tree_sha256,
                    )
                    loaded_model = WhisperModel(
                        model_source,
                        device=self.device,
                        compute_type=self.compute_type,
                    )
                    if self.model_path is not None:
                        # Recheck after config/tokenizer/model consumption. A
                        # mutation in the verification/load window must never
                        # publish a ready model generation.
                        _verify_stream_model_directory(
                            model_source,
                            self.model_sha256,
                            self.model_tree_sha256,
                        )
                    self._model = loaded_model

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def transcribe_array(
        self,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
        hotwords: str | None = None,
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
        measured_audio_rms = (
            float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0
        )
        audio_rms: float | None = measured_audio_rms if math.isfinite(measured_audio_rms) else None
        with self._lock:
            segments, _info = self._model.transcribe(  # type: ignore[attr-defined]
                audio,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=vad,
                vad_parameters=self.vad_parameters if vad else None,
                condition_on_previous_text=self.condition_on_previous_text,
                no_speech_threshold=self.no_speech_threshold,
                log_prob_threshold=self.log_prob_threshold,
                compression_ratio_threshold=self.compression_ratio_threshold,
                hotwords=hotwords,
            )
            return " ".join(
                str(segment.text).strip()
                for segment in segments
                if _usable_stream_segment(
                    segment,
                    self.no_speech_threshold,
                    self.log_prob_threshold,
                    audio_rms=audio_rms,
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
        model_path=cast(str | None, config["model_path"]),
        model_sha256=cast(str, config["model_sha256"]),
        model_tree_sha256=cast(str, config["model_tree_sha256"]),
        no_speech_threshold=cast(float, config["no_speech_threshold"]),
        log_prob_threshold=cast(float, config["log_prob_threshold"]),
        compression_ratio_threshold=cast(float, config["compression_ratio_threshold"]),
        condition_on_previous_text=cast(bool, config["condition_on_previous_text"]),
        vad_parameters=cast(dict[str, float | int], config["vad_parameters"]),
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
                raw_hotwords = task.get("hotwords")
                hotwords = raw_hotwords if isinstance(raw_hotwords, str) else None
                text = service.transcribe_array(
                    audio,
                    bool(task.get("vad", False)),
                    hotwords,
                )
            result_queue.put({"job_id": job_id, "ok": True, "text": text})
        except BaseException as exc:  # noqa: BLE001 - inference reports class only
            response: dict[str, object] = {
                "job_id": job_id,
                "ok": False,
                "error_class": type(exc).__name__,
            }
            if task.get("type") == "load":
                # A load failure happens before any audio has reached this
                # process, so its message cannot carry transcript content and
                # the KVKK boundary that keeps inference errors class-only does
                # not apply. Reporting only the class made a model-verification
                # failure surface as an opaque "RuntimeError: ValueError" with
                # no way to tell a bad path from a digest mismatch.
                response["error_detail"] = str(exc)[:200]
            result_queue.put(response)


class _SupervisedWhisperService:
    """Single-flight model with terminate/kill/respawn timeout semantics."""

    hard_timeout = True

    def __init__(self, settings: Settings, *, role: str) -> None:
        self.role = role
        is_final = role == "final"
        self._config: dict[str, object] = {
            "role": role,
            "model_name": (settings.final_model_name if is_final else settings.live_model_name),
            "model_path": (
                str(settings.final_model_path if is_final else settings.live_model_path)
                if (settings.final_model_path if is_final else settings.live_model_path) is not None
                else None
            ),
            "model_sha256": (
                settings.final_model_sha256 if is_final else settings.live_model_sha256
            ),
            "model_tree_sha256": (
                settings.final_model_tree_sha256 if is_final else settings.live_model_tree_sha256
            ),
            "device": settings.final_device if is_final else settings.live_device,
            "compute_type": (
                settings.final_compute_type if is_final else settings.live_compute_type
            ),
            "language": settings.language,
            "beam_size": (settings.final_beam_size if is_final else settings.live_beam_size),
            "no_speech_threshold": settings.no_speech_threshold,
            "log_prob_threshold": settings.log_prob_threshold,
            "compression_ratio_threshold": settings.compression_ratio_threshold,
            "condition_on_previous_text": settings.condition_on_previous_text,
            "vad_parameters": {
                "threshold": settings.stream_vad_threshold,
                "min_speech_duration_ms": settings.stream_vad_min_speech_duration_ms,
                "min_silence_duration_ms": settings.stream_vad_min_silence_duration_ms,
                "speech_pad_ms": settings.stream_vad_speech_pad_ms,
            },
        }
        self._timeout_sec = (
            settings.stream_final_timeout_sec if is_final else settings.stream_live_timeout_sec
        )
        self._load_timeout_sec = settings.stream_model_load_timeout_sec
        self._kill_grace_sec = settings.worker_kill_grace_sec
        self._ctx = mp.get_context("spawn")
        self._call_lock = threading.Lock()
        self._closing = threading.Event()
        self._process: Any | None = None
        self._task_queue: Any = None
        self._result_queue: Any = None
        self._model_loaded = False
        self._restart_blocked = False
        self._generation = 0
        self._start()

    def _start(self) -> None:
        if self._is_closing():
            raise WorkerCrashedError(f"streaming {self.role} worker is shutting down")
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

    def _is_closing(self) -> bool:
        closing = getattr(self, "_closing", None)
        return closing is not None and closing.is_set()

    @staticmethod
    def _close_queue(queue_object: Any) -> None:
        close = getattr(queue_object, "close", None)
        if callable(close):
            close()
        cancel_join_thread = getattr(queue_object, "cancel_join_thread", None)
        if callable(cancel_join_thread):
            cancel_join_thread()

    def _terminate_and_restart(
        self, *, restart: bool = True, deadline: float | None = None
    ) -> None:
        task_queue = self._task_queue
        result_queue = self._result_queue
        process = self._process
        # Invalidate readiness before touching the child. Even an unkillable
        # process must not leave the accepted stream's generation valid.
        self._model_loaded = False
        self._generation = getattr(self, "_generation", 0) + 1

        def join_timeout() -> float:
            if deadline is None:
                return self._kill_grace_sec
            return max(0.0, min(self._kill_grace_sec, deadline - time.monotonic()))

        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=join_timeout())
            if process.is_alive():
                process.kill()
                process.join(timeout=join_timeout())
            if process.is_alive():
                # Starting another GPU worker while the old native process is
                # still alive would make process/VRAM use unbounded. Keep the
                # supervisor permanently fail-closed until the service is
                # restarted by its orchestrator.
                self._restart_blocked = True
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker could not be stopped safely"
                )
        self._close_queue(task_queue)
        self._close_queue(result_queue)
        self._process = None
        if restart and not self._is_closing():
            if deadline is not None and time.monotonic() >= deadline:
                raise WorkerTimeoutError(
                    f"streaming {getattr(self, 'role', 'final')} worker restart exceeded timeout"
                )
            self._start()

    def _invoke_locked(
        self,
        operation: str,
        *,
        deadline: float,
        audio: bytes | None = None,
        vad: bool = False,
        hotwords: str | None = None,
        restart_on_failure: bool = True,
        required_generation: int | None = None,
        require_loaded: bool = False,
        cleanup_deadline: float | None = None,
    ) -> str:
        effective_cleanup_deadline = cleanup_deadline if operation == "load" else None
        if self._is_closing():
            raise WorkerCrashedError(
                f"streaming {getattr(self, 'role', 'final')} worker is shutting down"
            )
        if getattr(self, "_restart_blocked", False):
            raise WorkerCrashedError(
                f"streaming {getattr(self, 'role', 'final')} worker restart is blocked"
            )
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
            self._terminate_and_restart(deadline=effective_cleanup_deadline)
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
                    "hotwords": hotwords,
                },
                timeout=remaining,
            )
        except queue.Full as exc:
            self._terminate_and_restart(
                restart=restart_on_failure,
                deadline=effective_cleanup_deadline,
            )
            raise WorkerTimeoutError(
                f"streaming {getattr(self, 'role', 'final')} worker queue exceeded timeout"
            ) from exc
        while True:
            if self._is_closing():
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker is shutting down"
                )
            if not self._is_alive():
                self._terminate_and_restart(
                    restart=restart_on_failure,
                    deadline=effective_cleanup_deadline,
                )
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker exited before response"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_and_restart(
                    restart=restart_on_failure,
                    deadline=effective_cleanup_deadline,
                )
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
                error_class = str(response.get("error_class", "RuntimeError"))
                error_detail = str(response.get("error_detail", ""))
                error = RuntimeError(
                    f"{error_class}: {error_detail}" if error_detail else error_class
                )
                # Any native/CUDA failure can poison allocator or model state.
                # Invalidate this generation before surfacing the error. An
                # accepted stream passes restart_on_failure=False, so it never
                # retries or lazy-loads in the replacement generation.
                self._terminate_and_restart(
                    restart=restart_on_failure,
                    deadline=effective_cleanup_deadline,
                )
                raise error
            text = str(response.get("text", ""))
            if operation == "load":
                self._model_loaded = True
            return text

    def _acquire_call_lock(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._call_lock.acquire(timeout=remaining):
            raise WorkerTimeoutError(
                f"streaming {getattr(self, 'role', 'final')} worker queue exceeded timeout"
            )

    def _ensure_model_locked(
        self,
        deadline: float | None = None,
        cleanup_deadline: float | None = None,
    ) -> None:
        if self._model_loaded and self._is_alive():
            return
        effective_deadline = deadline
        if effective_deadline is None:
            effective_deadline = time.monotonic() + self._load_timeout_sec
        self._invoke_locked(
            "load",
            deadline=effective_deadline,
            cleanup_deadline=cleanup_deadline,
        )

    def ensure_model(
        self,
        *,
        deadline: float | None = None,
        cleanup_deadline: float | None = None,
    ) -> None:
        effective_deadline = deadline
        if effective_deadline is None:
            effective_deadline = time.monotonic() + self._load_timeout_sec
        effective_cleanup_deadline = cleanup_deadline
        if effective_cleanup_deadline is None:
            effective_cleanup_deadline = effective_deadline + (2 * self._kill_grace_sec)
        if effective_cleanup_deadline < effective_deadline:
            raise ValueError("cleanup deadline must not precede operation deadline")
        self._acquire_call_lock(effective_deadline)
        try:
            self._ensure_model_locked(effective_deadline, effective_cleanup_deadline)
        finally:
            self._call_lock.release()

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded and self._is_alive()

    @property
    def ready_generation(self) -> int:
        """Return the loaded worker generation under the single-flight lock."""
        deadline = time.monotonic() + self._load_timeout_sec
        self._acquire_call_lock(deadline)
        try:
            if not self._model_loaded or not self._is_alive():
                raise WorkerCrashedError(
                    f"streaming {getattr(self, 'role', 'final')} worker is not ready"
                )
            return self._generation
        finally:
            self._call_lock.release()

    def transcribe_array(
        self,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
        hotwords: str | None = None,
    ) -> str:
        contiguous = np.ascontiguousarray(audio, dtype=np.float32)
        queue_deadline = time.monotonic() + self._load_timeout_sec + self._timeout_sec
        self._acquire_call_lock(queue_deadline)
        try:
            # Readiness is rechecked after acquiring the shared single-flight lock.
            # A preceding timeout may have recycled the process while this call waited.
            self._ensure_model_locked(queue_deadline)
            ready_generation = self._generation
            return self._invoke_locked(
                "transcribe",
                deadline=time.monotonic() + self._timeout_sec,
                audio=contiguous.astype("<f4", copy=False).tobytes(),
                vad=vad,
                hotwords=hotwords,
                required_generation=ready_generation,
                require_loaded=True,
            )
        finally:
            self._call_lock.release()

    def transcribe_loaded_array(
        self,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
        expected_generation: int,
        hotwords: str | None = None,
    ) -> str:
        """Decode only with the worker generation proven ready for this stream.

        No inference inside an accepted connection may hide a cold model reload.
        If the supervised worker is recycled after readiness, that connection
        fails closed and lifecycle recovery must make a later connection ready.
        """
        contiguous = np.ascontiguousarray(audio, dtype=np.float32)
        deadline = time.monotonic() + self._timeout_sec
        self._acquire_call_lock(deadline)
        try:
            return self._invoke_locked(
                "transcribe",
                deadline=deadline,
                audio=contiguous.astype("<f4", copy=False).tobytes(),
                vad=vad,
                hotwords=hotwords,
                restart_on_failure=False,
                required_generation=expected_generation,
                require_loaded=True,
            )
        finally:
            self._call_lock.release()

    @property
    def healthy(self) -> bool:
        return not self._restart_blocked and not self._is_closing() and self._is_alive()

    def close(self, *, deadline: float) -> None:
        closing = getattr(self, "_closing", None)
        if closing is None:
            closing = threading.Event()
            self._closing = closing
        closing.set()
        self._acquire_call_lock(deadline)
        try:
            self._terminate_and_restart(restart=False, deadline=deadline)
        finally:
            self._call_lock.release()


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
    model_revision: str,
    model_sha256: str,
    model_tree_sha256: str,
    model_path: Path | None,
    no_speech_threshold: float,
    log_prob_threshold: float,
    compression_ratio_threshold: float,
    condition_on_previous_text: bool,
    vad_parameters: dict[str, float | int],
) -> DirectWhisperService:
    # Decode thresholds are part of the cache identity so a settings change
    # yields a fresh service rather than a stale cached one (#237).
    service_key = "\u0000".join(
        [
            key,
            model_name,
            model_revision,
            model_sha256,
            model_tree_sha256,
            str(model_path) if model_path is not None else "",
            device,
            compute_type,
            language,
            str(beam_size),
            str(no_speech_threshold),
            str(log_prob_threshold),
            str(compression_ratio_threshold),
            str(condition_on_previous_text),
            repr(sorted(vad_parameters.items())),
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
                model_path=str(model_path) if model_path is not None else None,
                model_sha256=model_sha256,
                model_tree_sha256=model_tree_sha256,
                no_speech_threshold=no_speech_threshold,
                log_prob_threshold=log_prob_threshold,
                compression_ratio_threshold=compression_ratio_threshold,
                condition_on_previous_text=condition_on_previous_text,
                vad_parameters=vad_parameters,
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
                settings.live_model_revision,
                settings.live_model_sha256,
                settings.live_model_tree_sha256,
                (str(settings.live_model_path) if settings.live_model_path is not None else ""),
                settings.live_device,
                settings.live_compute_type,
                settings.language,
                str(settings.live_beam_size),
                str(settings.stream_live_timeout_sec),
                str(settings.stream_model_load_timeout_sec),
                str(settings.no_speech_threshold),
                str(settings.log_prob_threshold),
                str(settings.compression_ratio_threshold),
                str(settings.condition_on_previous_text),
                str(settings.stream_vad_threshold),
                str(settings.stream_vad_min_speech_duration_ms),
                str(settings.stream_vad_min_silence_duration_ms),
                str(settings.stream_vad_speech_pad_ms),
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
        model_revision=settings.live_model_revision,
        model_sha256=settings.live_model_sha256,
        model_tree_sha256=settings.live_model_tree_sha256,
        model_path=settings.live_model_path,
        no_speech_threshold=settings.no_speech_threshold,
        log_prob_threshold=settings.log_prob_threshold,
        compression_ratio_threshold=settings.compression_ratio_threshold,
        condition_on_previous_text=settings.condition_on_previous_text,
        vad_parameters={
            "threshold": settings.stream_vad_threshold,
            "min_speech_duration_ms": settings.stream_vad_min_speech_duration_ms,
            "min_silence_duration_ms": settings.stream_vad_min_silence_duration_ms,
            "speech_pad_ms": settings.stream_vad_speech_pad_ms,
        },
    )


def get_final_service(
    settings: Settings,
) -> DirectWhisperService | SupervisedFinalWhisperService:
    """Accurate final model (ADR-0031: large-v3-turbo)."""
    if settings.stream_final_worker_backend == "process":
        service_key = "\u0000".join(
            [
                settings.final_model_name,
                settings.final_model_revision,
                settings.final_model_sha256,
                settings.final_model_tree_sha256,
                (str(settings.final_model_path) if settings.final_model_path is not None else ""),
                settings.final_device,
                settings.final_compute_type,
                settings.language,
                str(settings.final_beam_size),
                str(settings.stream_final_timeout_sec),
                str(settings.stream_model_load_timeout_sec),
                str(settings.no_speech_threshold),
                str(settings.log_prob_threshold),
                str(settings.compression_ratio_threshold),
                str(settings.condition_on_previous_text),
                str(settings.stream_vad_threshold),
                str(settings.stream_vad_min_speech_duration_ms),
                str(settings.stream_vad_min_silence_duration_ms),
                str(settings.stream_vad_speech_pad_ms),
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
        model_revision=settings.final_model_revision,
        model_sha256=settings.final_model_sha256,
        model_tree_sha256=settings.final_model_tree_sha256,
        model_path=settings.final_model_path,
        no_speech_threshold=settings.no_speech_threshold,
        log_prob_threshold=settings.log_prob_threshold,
        compression_ratio_threshold=settings.compression_ratio_threshold,
        condition_on_previous_text=settings.condition_on_previous_text,
        vad_parameters={
            "threshold": settings.stream_vad_threshold,
            "min_speech_duration_ms": settings.stream_vad_min_speech_duration_ms,
            "min_silence_duration_ms": settings.stream_vad_min_silence_duration_ms,
            "speech_pad_ms": settings.stream_vad_speech_pad_ms,
        },
    )


def streaming_services_healthy() -> bool:
    with _services_lock:
        supervised = [
            *_supervised_live_services.values(),
            *_supervised_final_services.values(),
        ]
        return all(service.healthy and service.model_loaded for service in supervised)


def shutdown_streaming_services(*, timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec
    with _services_lock:
        supervised = [
            *_supervised_live_services.values(),
            *_supervised_final_services.values(),
        ]
    closed: list[_SupervisedWhisperService] = []
    failures: list[Exception] = []
    for index, service in enumerate(supervised):
        remaining_services = len(supervised) - index
        remaining_total = max(0.0, deadline - time.monotonic())
        service_deadline = time.monotonic() + (remaining_total / remaining_services)
        try:
            service.close(deadline=service_deadline)
            closed.append(service)
        except Exception as exc:  # noqa: BLE001 - all workers still get a close attempt
            failures.append(exc)
            logger.error(
                "Streaming worker shutdown failed role=%s err=%s",
                service.role,
                type(exc).__name__,
            )
    with _services_lock:
        for key, service in list(_supervised_live_services.items()):
            if service in closed:
                del _supervised_live_services[key]
        for key, service in list(_supervised_final_services.items()):
            if service in closed:
                del _supervised_final_services[key]
    if failures:
        raise WorkerCrashedError(
            f"{len(failures)} streaming worker(s) did not stop within the shutdown deadline"
        ) from failures[0]
