"""WebSocket live streaming — two-stage GPU draft + final (#128).

Ported from the working GPU demo (commit d79e905). Flow per connection:

1. Client streams float32 PCM16k chunks over `/ws/stream`.
2. An RMS gate skips silence; every `STT_LIVE_INFER_INTERVAL_MS` the *live*
   model transcribes the last `STT_LIVE_WINDOW_SEC` seconds -> same-seq
   `partial` event. Defaults favor word-progressive UX while keeping the final
   pass authoritative.
3. On forced-commit age (`STT_FORCED_COMMIT_SEC`) the *final* model
   re-transcribes the whole buffer -> `final` event; speech-ending silence can
   also commit early via `STT_SILENCE_COMMIT_SEC`. A tail overlap is kept so
   words on the boundary are not lost.
4. Hallucination filter suppresses classic Whisper artefacts.

KVKK: server-side logs and debug events are transcript-free by default.
`STT_STREAM_DEBUG=true` enables verbose debug events (lengths/timings only —
the transcript itself travels solely in the client-facing WS payload).

Architecture note: the gateway-mediated path (client -> audio-gateway -> Redis
-> live-stt, ADR-0031 §3.7) remains the production target; this direct WS
endpoint is the PoC/dev path pending maintainer decision (issue #128).
"""

from __future__ import annotations

import contextlib
import logging
import time

import numpy as np
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.services.hallucination import is_hallucination
from app.services.streaming_models import get_final_service, get_live_service

router = APIRouter()
logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def _audio_rms(audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]]) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio**2)))


@router.websocket("/ws/stream")
async def stream_endpoint(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> None:
    await websocket.accept()

    live_service = get_live_service(settings)
    final_service = get_final_service(settings)
    debug_enabled = settings.stream_debug
    min_infer_samples = int(settings.min_infer_sec * SAMPLE_RATE)

    try:
        await websocket.send_json({"type": "loading", "stage": "live_model"})
        await run_in_threadpool(live_service.ensure_model)
        await websocket.send_json({"type": "loading", "stage": "final_model"})
        await run_in_threadpool(final_service.ensure_model)
    except WebSocketDisconnect:
        logger.info("WS disconnected during model load")
        return
    except Exception as exc:
        logger.exception("Streaming model load error")
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "msg": type(exc).__name__})
            await websocket.close()
        return

    await websocket.send_json(
        {
            "type": "ready",
            "sample_rate": SAMPLE_RATE,
            "live_model": settings.live_model_name,
            "final_model": settings.final_model_name,
        }
    )
    logger.info(
        "Stream connected",
        extra={"live_model": settings.live_model_name, "final_model": settings.final_model_name},
    )

    buffer: np.ndarray[tuple[int, ...], np.dtype[np.float32]] = np.zeros(0, dtype=np.float32)
    buffer_start_t: float | None = None
    last_live_infer_t = 0.0
    last_debug_t = 0.0
    seg_index = 0
    last_draft = ""
    sent_draft = ""
    pcm_chunks = 0
    speech_seen = False
    last_speech_t: float | None = None

    async def send_debug(event: str, **payload: object) -> None:
        # Transcript-free diagnostics; opt-in only (KVKK log discipline, #30).
        if not debug_enabled:
            return
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "debug", "event": event, **payload})

    async def commit_current(reason: str) -> None:
        nonlocal buffer, buffer_start_t, seg_index, last_draft, sent_draft, speech_seen
        nonlocal last_speech_t

        buffer_sec = round(buffer.size / SAMPLE_RATE, 2)
        if buffer.size < min_infer_samples:
            await send_debug("final_skip_short_buffer", buffer_sec=buffer_sec)
            return

        audio = buffer.copy()
        rms = _audio_rms(audio)
        if rms < settings.min_speech_rms:
            await send_debug("final_skip_low_rms", rms=round(rms, 5), buffer_sec=buffer_sec)
            return

        await send_debug("final_start", reason=reason, rms=round(rms, 5), buffer_sec=buffer_sec)
        started = time.perf_counter()
        try:
            text = await run_in_threadpool(final_service.transcribe_array, audio, True)
        except Exception as exc:  # noqa: BLE001 - keep stream alive, fall back to draft
            # exc_info is transcript-free (code paths only) — KVKK-safe diagnostics.
            logger.warning("Final pass error err_class=%s", type(exc).__name__, exc_info=True)
            await send_debug("final_error", error=type(exc).__name__)
            text = last_draft

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if not text or is_hallucination(text):
            await send_debug("final_filtered", elapsed_ms=elapsed_ms, buffer_sec=buffer_sec)
            return

        await websocket.send_json(
            {
                "type": "final",
                "seq": seg_index,
                "text": text,
                "reason": reason,
                "elapsed_ms": elapsed_ms,
                "rms": round(rms, 5),
            }
        )
        await send_debug("final_sent", seq=seg_index, elapsed_ms=elapsed_ms, text_len=len(text))
        # KVKK: no transcript content in server logs.
        logger.info(
            "Final segment sent",
            extra={"seq": seg_index, "reason": reason, "elapsed_ms": elapsed_ms},
        )

        seg_index += 1
        last_draft = ""
        sent_draft = ""
        speech_seen = False
        last_speech_t = None

        tail_samples = int(settings.tail_overlap_sec * SAMPLE_RATE)
        buffer = (
            buffer[-tail_samples:]
            if tail_samples > 0 and buffer.shape[0] > tail_samples
            else np.zeros(0, dtype=np.float32)
        )
        buffer_start_t = time.time() if buffer.size else None

    try:
        while True:
            data = await websocket.receive_bytes()
            if not data:
                continue

            samples = np.frombuffer(data, dtype=np.float32)
            if samples.size == 0:
                continue

            pcm_chunks += 1
            now = time.time()
            if buffer_start_t is None:
                buffer_start_t = now

            buffer = np.concatenate([buffer, samples])
            max_samples = int(settings.final_window_sec * SAMPLE_RATE)
            if buffer.shape[0] > max_samples:
                buffer = buffer[-max_samples:]

            sample_rms = _audio_rms(samples)
            buffer_age = now - buffer_start_t

            if now - last_debug_t >= settings.debug_every_sec:
                await send_debug(
                    "audio_tick",
                    chunks=pcm_chunks,
                    sample_rms=round(sample_rms, 5),
                    buffer_sec=round(buffer.size / SAMPLE_RATE, 2),
                    buffer_age=round(buffer_age, 2),
                    has_draft=bool(last_draft),
                )
                last_debug_t = now

            if buffer_age >= settings.forced_commit_sec and speech_seen:
                await commit_current("forced")
                continue

            if sample_rms >= settings.silence_rms:
                speech_seen = True
                last_speech_t = now
            else:
                if (
                    speech_seen
                    and last_speech_t is not None
                    and now - last_speech_t >= settings.silence_commit_sec
                ):
                    await commit_current("silence")
                continue

            if (now - last_live_infer_t) * 1000 < settings.live_infer_interval_ms:
                continue
            last_live_infer_t = now

            live_samples = int(settings.live_window_sec * SAMPLE_RATE)
            live_audio = (
                buffer[-live_samples:].copy() if buffer.shape[0] > live_samples else buffer.copy()
            )

            if live_audio.size < min_infer_samples:
                await send_debug("draft_skip_short_buffer")
                continue

            live_rms = _audio_rms(live_audio)
            if live_rms < settings.min_speech_rms:
                await send_debug("draft_skip_low_rms", rms=round(live_rms, 5))
                continue

            await send_debug("draft_start", rms=round(live_rms, 5))
            started = time.perf_counter()
            try:
                draft = await run_in_threadpool(live_service.transcribe_array, live_audio, False)
            except Exception as exc:  # noqa: BLE001 - skip this tick, keep stream alive
                # exc_info is transcript-free (code paths only) — KVKK-safe diagnostics.
                logger.warning("Draft pass error err_class=%s", type(exc).__name__, exc_info=True)
                await send_debug("draft_error", error=type(exc).__name__)
                continue

            elapsed_ms = int((time.perf_counter() - started) * 1000)

            if not draft or is_hallucination(draft):
                await send_debug("draft_filtered", elapsed_ms=elapsed_ms)
                continue

            last_draft = draft
            if draft != sent_draft:
                await websocket.send_json(
                    {
                        "type": "partial",
                        "seq": seg_index,
                        "confirmed": "",
                        "tentative": draft,
                        "elapsed_ms": elapsed_ms,
                        "rms": round(live_rms, 5),
                        "source": settings.live_model_name,
                    }
                )
                await send_debug(
                    "draft_sent", seq=seg_index, elapsed_ms=elapsed_ms, text_len=len(draft)
                )
                sent_draft = draft

    except WebSocketDisconnect:
        logger.info("WS disconnected")
    except Exception:
        logger.exception("WS stream error")
        with contextlib.suppress(Exception):
            await websocket.close()
