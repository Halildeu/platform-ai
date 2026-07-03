"""WebSocket live streaming — two-stage GPU draft + final (#128).

Ported from the working GPU demo (commit d79e905). Flow per connection:

1. Client streams float32 PCM16k chunks over `/ws/stream`.
2. An RMS gate skips silence; every `STT_LIVE_INFER_INTERVAL_MS` the *live*
   model transcribes the last `STT_LIVE_WINDOW_SEC` seconds -> same-seq
   `partial` event. Defaults favor word-progressive UX while keeping the final
   pass authoritative.
3. On forced-commit age (`STT_FORCED_COMMIT_SEC`) the *final* model
   re-transcribes the whole buffer -> `final` event; speech-ending silence can
   also commit early via `STT_SILENCE_COMMIT_SEC`. A tail overlap is kept only
   for forced commits so words on continuous-speech boundaries are not lost.
4. Hallucination filter suppresses classic Whisper artefacts.

KVKK: server-side logs and debug events are transcript-free by default.
`STT_STREAM_DEBUG=true` enables verbose debug events (lengths/timings only —
the transcript itself travels solely in the client-facing WS payload).

Architecture note: the gateway-mediated path (client -> audio-gateway -> Redis
-> live-stt, ADR-0031 §3.7) remains the production target; this direct WS
endpoint is the PoC/dev path pending maintainer decision (issue #128).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
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
_WORD_RE = re.compile(r"[\wçğıöşüÇĞİÖŞÜ]+", re.UNICODE)
MIN_FALLBACK_DRAFT_WORDS = 2
MAX_RECENT_FINAL_WORDS = 24
ROLLING_CONTINUATION_MIN_PREVIOUS_WORDS = 4
ROLLING_CONTINUATION_MIN_NEXT_WORDS = 1
_OVERLAP_SUFFIXES = (
    "lerinizden",
    "larınızdan",
    "lerinizde",
    "larınızda",
    "lerinizin",
    "larınızın",
    "leriniz",
    "larınız",
    "lerimin",
    "larımın",
    "lerimi",
    "larımı",
    "lerim",
    "larım",
    "sının",
    "sinin",
    "sunun",
    "sünün",
    "ının",
    "inin",
    "unun",
    "ünün",
    "sını",
    "sini",
    "sunu",
    "sünü",
    "ımız",
    "imiz",
    "umuz",
    "ümüz",
    "imin",
    "ımın",
    "umun",
    "ümün",
    "nın",
    "nin",
    "nun",
    "nün",
    "mın",
    "min",
    "mun",
    "mün",
    "ını",
    "ini",
    "unu",
    "ünü",
    "nı",
    "ni",
    "nu",
    "nü",
    "yı",
    "yi",
    "yu",
    "yü",
    "sı",
    "si",
    "su",
    "sü",
    "ı",
    "i",
    "u",
    "ü",
)


def _audio_rms(audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]]) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio**2)))


def _word_count(text: str) -> int:
    return len((text or "").split())


def _normalized_words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD_RE.findall(text or "")]


def _shared_token_ratio(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    denominator = min(len(left_set), len(right_set))
    if denominator == 0:
        return 0.0
    return len(left_set & right_set) / denominator


def _has_same_prefix(previous_text: str, next_text: str) -> bool:
    return next_text.casefold().startswith(previous_text.casefold())


def _contiguous_index(haystack: list[str], needle: list[str]) -> int:
    if not needle or len(needle) > len(haystack):
        return -1

    for start in range(0, len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return start

    return -1


def _suffix_prefix_overlap(previous_words: list[str], next_words: list[str]) -> int:
    max_overlap = min(len(previous_words), len(next_words))
    for size in range(max_overlap, 0, -1):
        if previous_words[-size:] == next_words[:size]:
            return size

    return 0


def _overlap_family(word: str) -> str:
    family = word
    for _ in range(2):
        for suffix in _OVERLAP_SUFFIXES:
            if len(family) > len(suffix) + 2 and family.endswith(suffix):
                family = family[: -len(suffix)]
                break
        else:
            break
    return family


def _overlap_families(words: list[str]) -> list[str]:
    return [_overlap_family(word) for word in words]


def _trim_to_active_audio(
    samples: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
    threshold: float,
) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
    if samples.size == 0:
        return samples

    active_offsets = np.flatnonzero(np.abs(samples) >= threshold)
    if active_offsets.size == 0:
        return np.zeros(0, dtype=np.float32)
    return samples[int(active_offsets[0]) : int(active_offsets[-1]) + 1].copy()


def _suffix_prefix_speech_overlap(previous_words: list[str], next_words: list[str]) -> int:
    exact_overlap = _suffix_prefix_overlap(previous_words, next_words)
    if exact_overlap > 0:
        return exact_overlap

    # Whisper often repeats the previous segment head with Turkish case or
    # possessive suffixes changed. Only use the fuzzy form for multi-word
    # overlap so intentional single-word repeats are not erased.
    fuzzy_overlap = _suffix_prefix_overlap(
        _overlap_families(previous_words),
        _overlap_families(next_words),
    )
    return fuzzy_overlap if fuzzy_overlap >= 2 else 0


def _merge_rolling_partial(previous_text: str, next_text: str) -> str:
    """Merge overlapping rolling-window drafts without losing earlier words."""
    previous = (previous_text or "").strip()
    next_candidate = (next_text or "").strip()
    if not previous or not next_candidate:
        return next_candidate or previous
    if previous == next_candidate or _has_same_prefix(previous, next_candidate):
        return next_candidate
    if _has_same_prefix(next_candidate, previous):
        return previous

    previous_raw_words = previous.split()
    next_raw_words = next_candidate.split()
    previous_words = _normalized_words(previous)
    next_words = _normalized_words(next_candidate)

    if _contiguous_index(previous_words, next_words) >= 0:
        return previous

    overlap = _suffix_prefix_speech_overlap(previous_words, next_words)
    if overlap > 0:
        return " ".join([*previous_raw_words, *next_raw_words[overlap:]])

    if previous_words[0] == next_words[0]:
        return next_candidate

    shared_family_ratio = _shared_token_ratio(
        _overlap_families(previous_words),
        _overlap_families(next_words),
    )
    next_looks_like_continuation = (
        len(previous_words) >= ROLLING_CONTINUATION_MIN_PREVIOUS_WORDS
        and len(next_words) >= ROLLING_CONTINUATION_MIN_NEXT_WORDS
        and shared_family_ratio < 0.5
    )
    next_looks_like_growing_window = (
        len(next_words) > len(previous_words)
        and len(next_words) >= 3
        and (len(previous_words) >= 2 or len(next_words) >= len(previous_words) + 2)
    )

    if next_looks_like_continuation or next_looks_like_growing_window:
        return " ".join([*previous_raw_words, *next_raw_words])

    return next_candidate


def _merge_final_transcript(previous_text: str, final_text: str) -> str:
    """Apply final revisions without dropping already displayed rolling context."""
    previous = (previous_text or "").strip()
    final = (final_text or "").strip()
    if not previous or not final:
        return final or previous
    if previous == final or _has_same_prefix(previous, final):
        return final
    if _has_same_prefix(final, previous):
        return previous

    previous_raw_words = previous.split()
    final_raw_words = final.split()
    previous_words = _normalized_words(previous)
    final_words = _normalized_words(final)

    contained_at = _contiguous_index(previous_words, final_words)
    if contained_at >= 0:
        return " ".join(
            [
                *previous_raw_words[:contained_at],
                *final_raw_words,
                *previous_raw_words[contained_at + len(final_raw_words) :],
            ]
        )
    if _contiguous_index(final_words, previous_words) >= 0:
        return final

    overlap = _suffix_prefix_speech_overlap(previous_words, final_words)
    if overlap >= 2:
        return " ".join([*previous_raw_words, *final_raw_words[overlap:]])

    if len(final_words) <= len(previous_words) + 1 and len(final_words) <= 3:
        return previous

    return final


def _drop_leading_tail_overlap(
    previous_text: str, next_text: str, *, allow_single_word: bool = False
) -> str:
    """Remove cross-segment tail carry-over from the next final payload."""
    previous = (previous_text or "").strip()
    next_candidate = (next_text or "").strip()
    if not previous or not next_candidate:
        return next_candidate

    next_raw_words = next_candidate.split()
    previous_words = _normalized_words(previous)
    next_words = _normalized_words(next_candidate)
    overlap = _suffix_prefix_speech_overlap(previous_words, next_words)
    if overlap <= 0:
        return next_candidate
    if overlap == 1 and len(previous_words) > 1 and not allow_single_word:
        return next_candidate
    if overlap >= len(next_raw_words):
        return next_candidate
    return " ".join(next_raw_words[overlap:]).strip()


def _append_recent_final_text(previous_text: str, emitted_text: str) -> str:
    """Keep a bounded final transcript tail for cross-segment overlap cleanup."""
    words = [*(previous_text or "").split(), *(emitted_text or "").split()]
    return " ".join(words[-MAX_RECENT_FINAL_WORDS:])


def _select_partial_text(draft: str, sent_draft: str) -> str | None:
    """Keep live drafts word-progressive so the UI does not erase spoken words."""
    candidate = (draft or "").strip()
    previous = (sent_draft or "").strip()

    if not candidate or candidate == previous:
        return None
    if not previous:
        return candidate

    candidate = _merge_rolling_partial(previous, candidate)
    if candidate == previous:
        return None

    if _word_count(candidate) < _word_count(previous):
        return None
    if is_hallucination(candidate):
        return None

    return candidate


def _select_commit_text(final_text: str, fallback_draft: str) -> str | None:
    """Choose a KVKK-safe final payload without letting hallucinations poison state."""
    candidate = (final_text or "").strip()
    fallback = (fallback_draft or "").strip()
    fallback_ok = bool(fallback and not is_hallucination(fallback))
    short_final_artifact = bool(
        candidate and is_hallucination(candidate) and _word_count(candidate) <= 1
    )

    if candidate and not is_hallucination(candidate):
        merged = _merge_final_transcript(fallback, candidate) if fallback_ok else candidate
        if merged and not is_hallucination(merged):
            return merged
        return candidate

    if fallback_ok and (short_final_artifact or _word_count(fallback) >= MIN_FALLBACK_DRAFT_WORDS):
        return fallback

    return None


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
    last_final_text = ""
    recent_final_text = ""
    total_samples_received = 0
    buffer_start_sample = 0
    buffer_lock = asyncio.Lock()
    send_lock = asyncio.Lock()
    stop_inference = asyncio.Event()

    async def send_debug(event: str, **payload: object) -> None:
        # Transcript-free diagnostics; opt-in only (KVKK log discipline, #30).
        if not debug_enabled:
            return
        with contextlib.suppress(Exception):
            async with send_lock:
                await websocket.send_json({"type": "debug", "event": event, **payload})

    async def send_json(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def commit_current(reason: str) -> None:
        nonlocal buffer, buffer_start_t, seg_index, last_draft, sent_draft, speech_seen
        nonlocal last_speech_t, last_final_text, recent_final_text, buffer_start_sample

        def trim_leading_silence(
            samples: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        ) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
            active = _trim_to_active_audio(samples, settings.silence_rms)
            return active if active.size == 0 else active.copy()

        async def advance_segment(
            *,
            retain_tail: bool,
            commit_end_sample: int,
            committed_audio: np.ndarray,
        ) -> None:
            nonlocal buffer, buffer_start_t, seg_index, last_draft, sent_draft, speech_seen
            nonlocal last_speech_t, buffer_start_sample

            async with buffer_lock:
                seg_index += 1
                last_draft = ""
                sent_draft = ""

                # The receive loop may trim the rolling buffer while the final
                # model is still decoding. Use absolute sample coordinates so
                # audio received during that slow final pass is not mistaken for
                # already-committed audio and dropped.
                appended_start = max(0, commit_end_sample - buffer_start_sample)
                appended = (
                    buffer[appended_start:].copy()
                    if appended_start < buffer.shape[0]
                    else np.zeros(0, dtype=np.float32)
                )
                if not retain_tail:
                    appended = trim_leading_silence(appended)
                tail_samples = int(settings.tail_overlap_sec * SAMPLE_RATE) if retain_tail else 0
                tail = (
                    committed_audio[-tail_samples:].copy()
                    if tail_samples > 0 and committed_audio.shape[0] > tail_samples
                    else np.zeros(0, dtype=np.float32)
                )
                buffer = (
                    np.concatenate([tail, appended])
                    if tail.size or appended.size
                    else np.zeros(0, dtype=np.float32)
                )
                max_samples = int(settings.final_window_sec * SAMPLE_RATE)
                if buffer.shape[0] > max_samples:
                    buffer = buffer[-max_samples:]

                now = time.time()
                buffer_start_t = now - (buffer.size / SAMPLE_RATE) if buffer.size else None
                buffer_start_sample = max(0, total_samples_received - buffer.shape[0])
                residual_rms = _audio_rms(buffer)
                speech_seen = bool(buffer.size and residual_rms >= settings.silence_rms)
                last_speech_t = now if speech_seen else None

        async with buffer_lock:
            audio = buffer.copy()
            active_seq = seg_index
            commit_end_sample = buffer_start_sample + buffer.shape[0]

        if reason == "silence":
            audio = trim_leading_silence(audio)

        buffer_sec = round(audio.size / SAMPLE_RATE, 2)
        if audio.size < min_infer_samples:
            await send_debug("final_skip_short_buffer", buffer_sec=buffer_sec)
            await advance_segment(
                retain_tail=False,
                commit_end_sample=commit_end_sample,
                committed_audio=audio,
            )
            return

        rms = _audio_rms(audio)
        if rms < settings.min_speech_rms:
            await send_debug("final_skip_low_rms", rms=round(rms, 5), buffer_sec=buffer_sec)
            await advance_segment(
                retain_tail=False,
                commit_end_sample=commit_end_sample,
                committed_audio=audio,
            )
            return

        await send_debug("final_start", reason=reason, rms=round(rms, 5), buffer_sec=buffer_sec)
        started = time.perf_counter()
        try:
            text = await run_in_threadpool(
                final_service.transcribe_array,
                audio,
                settings.stream_final_vad_filter,
            )
        except Exception as exc:  # noqa: BLE001 - keep stream alive, fall back to draft
            # exc_info is transcript-free (code paths only) — KVKK-safe diagnostics.
            logger.warning("Final pass error err_class=%s", type(exc).__name__, exc_info=True)
            await send_debug("final_error", error=type(exc).__name__)
            text = last_draft

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        selected_text = _select_commit_text(text, last_draft)

        if selected_text is None:
            await send_debug("final_filtered", elapsed_ms=elapsed_ms, buffer_sec=buffer_sec)
            await advance_segment(
                retain_tail=False,
                commit_end_sample=commit_end_sample,
                committed_audio=audio,
            )
            return

        if selected_text != text:
            await send_debug("final_fallback_draft", elapsed_ms=elapsed_ms, buffer_sec=buffer_sec)

        text = selected_text
        text = _drop_leading_tail_overlap(
            recent_final_text or last_final_text,
            text,
            allow_single_word=True,
        )
        if not text:
            await send_debug("final_dropped_tail_only", seq=active_seq, elapsed_ms=elapsed_ms)
            await advance_segment(
                retain_tail=False,
                commit_end_sample=commit_end_sample,
                committed_audio=audio,
            )
            return

        await send_json(
            {
                "type": "final",
                "seq": active_seq,
                "text": text,
                "reason": reason,
                "elapsed_ms": elapsed_ms,
                "rms": round(rms, 5),
            }
        )
        await send_debug("final_sent", seq=active_seq, elapsed_ms=elapsed_ms, text_len=len(text))
        # KVKK: no transcript content in server logs.
        logger.info(
            "Final segment sent",
            extra={"seq": active_seq, "reason": reason, "elapsed_ms": elapsed_ms},
        )
        last_final_text = text
        recent_final_text = _append_recent_final_text(recent_final_text, text)

        # A forced commit happens while speech is still continuous, so a small
        # tail helps avoid boundary loss. A silence commit already has an
        # utterance boundary; carrying tail there pollutes the next segment with
        # the previous words and creates repeated alternatives in practice.
        await advance_segment(
            retain_tail=reason == "forced",
            commit_end_sample=commit_end_sample,
            committed_audio=audio,
        )

    async def infer_live_partial() -> None:
        nonlocal last_draft, sent_draft

        async with buffer_lock:
            live_samples = int(settings.live_window_sec * SAMPLE_RATE)
            live_audio = (
                buffer[-live_samples:].copy() if buffer.shape[0] > live_samples else buffer.copy()
            )
            active_seq = seg_index

        live_audio = _trim_to_active_audio(live_audio, settings.silence_rms)
        if live_audio.size < min_infer_samples:
            await send_debug("draft_skip_short_buffer")
            return

        live_rms = _audio_rms(live_audio)
        if live_rms < settings.min_speech_rms:
            await send_debug("draft_skip_low_rms", rms=round(live_rms, 5))
            return

        await send_debug("draft_start", rms=round(live_rms, 5))
        started = time.perf_counter()
        try:
            draft = await run_in_threadpool(live_service.transcribe_array, live_audio, False)
        except Exception as exc:  # noqa: BLE001 - skip this tick, keep stream alive
            # exc_info is transcript-free (code paths only) — KVKK-safe diagnostics.
            logger.warning("Draft pass error err_class=%s", type(exc).__name__, exc_info=True)
            await send_debug("draft_error", error=type(exc).__name__)
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if not draft or is_hallucination(draft):
            await send_debug("draft_filtered", elapsed_ms=elapsed_ms)
            return

        selected_draft = _select_partial_text(draft, sent_draft)
        if selected_draft is None:
            await send_debug(
                "draft_regression_filtered",
                elapsed_ms=elapsed_ms,
                previous_words=_word_count(sent_draft),
                candidate_words=_word_count(draft),
            )
            return

        last_draft = selected_draft
        if selected_draft != sent_draft:
            await send_json(
                {
                    "type": "partial",
                    "seq": active_seq,
                    "confirmed": "",
                    "tentative": selected_draft,
                    "elapsed_ms": elapsed_ms,
                    "rms": round(live_rms, 5),
                    "source": settings.live_model_name,
                }
            )
            await send_debug(
                "draft_sent",
                seq=active_seq,
                elapsed_ms=elapsed_ms,
                text_len=len(draft),
            )
            sent_draft = selected_draft

    async def inference_loop() -> None:
        nonlocal last_live_infer_t

        while not stop_inference.is_set():
            await asyncio.sleep(0.05)
            now = time.time()
            commit_reason: str | None = None

            async with buffer_lock:
                if not speech_seen or buffer_start_t is None:
                    continue

                buffer_age = now - buffer_start_t
                if buffer_age >= settings.forced_commit_sec:
                    commit_reason = "forced"
                elif (
                    last_speech_t is not None and now - last_speech_t >= settings.silence_commit_sec
                ):
                    commit_reason = "silence"

                should_infer = (
                    commit_reason is None
                    and (now - last_live_infer_t) * 1000 >= settings.live_infer_interval_ms
                )
                if should_infer:
                    last_live_infer_t = now

            if commit_reason is not None:
                await commit_current(commit_reason)
                last_live_infer_t = time.time()
                continue

            if should_infer:
                await infer_live_partial()
                last_live_infer_t = time.time()

    inference_task = asyncio.create_task(inference_loop())
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
            sample_rms = _audio_rms(samples)
            debug_payload: dict[str, object] | None = None

            async with buffer_lock:
                total_samples_received += int(samples.size)
                if buffer_start_t is None:
                    buffer_start_t = now

                buffer = np.concatenate([buffer, samples])
                max_samples = int(settings.final_window_sec * SAMPLE_RATE)
                if buffer.shape[0] > max_samples:
                    buffer = buffer[-max_samples:]
                    buffer_start_t = now - (buffer.size / SAMPLE_RATE)
                buffer_start_sample = max(0, total_samples_received - buffer.shape[0])

                buffer_age = now - buffer_start_t
                if sample_rms >= settings.silence_rms:
                    speech_seen = True
                    last_speech_t = now

                if now - last_debug_t >= settings.debug_every_sec:
                    debug_payload = {
                        "chunks": pcm_chunks,
                        "sample_rms": round(sample_rms, 5),
                        "buffer_sec": round(buffer.size / SAMPLE_RATE, 2),
                        "buffer_age": round(buffer_age, 2),
                        "has_draft": bool(last_draft),
                    }
                    last_debug_t = now

            if debug_payload is not None:
                await send_debug("audio_tick", **debug_payload)

    except WebSocketDisconnect:
        logger.info("WS disconnected")
    except Exception:
        logger.exception("WS stream error")
        with contextlib.suppress(Exception):
            await websocket.close()
    finally:
        stop_inference.set()
        inference_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await inference_task
