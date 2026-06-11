"""Two-stage streaming model services (#128).

The WebSocket streaming path uses two direct, in-process Whisper models —
a fast *live draft* model and an accurate *final revision* model — instead of
the request/response worker pool (`worker.py`). Rationale: streaming needs
sub-second repeated inference over a rolling buffer; per-request process
dispatch would dominate latency. The models are lazy-loaded once and inference
is single-flight per model behind a lock.

Defaults follow ADR-0031: draft = `medium` int8, final = `large-v3-turbo`
(fp16). Both are intended for the GPU host; the module loads nothing at import
time, so CPU/CI environments are unaffected unless `/ws/stream` is used.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from app.core.config import Settings

logger = logging.getLogger(__name__)


class DirectWhisperService:
    """Lazy-loaded, lock-guarded faster-whisper wrapper for streaming."""

    def __init__(self, model_name: str, device: str, compute_type: str, language: str) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
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

        Decode thresholds follow the GPU demo tuning: no cross-window prompt
        conditioning (rolling buffer), aggressive no-speech suppression.
        """
        self.ensure_model()
        assert self._model is not None
        with self._lock:
            segments, _info = self._model.transcribe(  # type: ignore[attr-defined]
                audio,
                language=self.language,
                beam_size=1,
                vad_filter=vad,
                condition_on_previous_text=False,
                no_speech_threshold=0.75,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )
            return " ".join(s.text.strip() for s in segments).strip()


_services: dict[str, DirectWhisperService] = {}
_services_lock = threading.Lock()


def _named(
    key: str, model_name: str, device: str, compute_type: str, language: str
) -> DirectWhisperService:
    with _services_lock:
        if key not in _services:
            _services[key] = DirectWhisperService(model_name, device, compute_type, language)
        return _services[key]


def get_live_service(settings: Settings) -> DirectWhisperService:
    """Fast draft model (ADR-0031: medium int8)."""
    return _named(
        "live",
        settings.live_model_name,
        settings.live_device,
        settings.live_compute_type,
        settings.language,
    )


def get_final_service(settings: Settings) -> DirectWhisperService:
    """Accurate final model (ADR-0031: large-v3-turbo)."""
    return _named(
        "final",
        settings.final_model_name,
        settings.final_device,
        settings.final_compute_type,
        settings.language,
    )
