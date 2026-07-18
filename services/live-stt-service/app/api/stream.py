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
import json
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
SHORT_FINAL_PRESERVE_MIN_PREVIOUS_WORDS = 5
SHORT_FINAL_PRESERVE_MAX_RATIO = 0.65
SHORT_FINAL_PRESERVE_MAX_SHARED_RATIO = 0.45
EOF_CONTROL = {"type": "eof"}
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


def _common_prefix_size(left_words: list[str], right_words: list[str]) -> int:
    size = 0
    for left, right in zip(left_words, right_words, strict=False):
        if left != right:
            break
        size += 1
    return size


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
        # Same-opener hypotheses normally cover the same rolling audio. Appending
        # unrelated tails fabricates a sentence from competing ASR alternatives.
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

    shared_family_ratio = _shared_token_ratio(
        _overlap_families(previous_words),
        _overlap_families(final_words),
    )
    final_looks_like_short_alternative = (
        len(previous_words) >= SHORT_FINAL_PRESERVE_MIN_PREVIOUS_WORDS
        and len(final_words) < len(previous_words)
        and len(final_words) / len(previous_words) <= SHORT_FINAL_PRESERVE_MAX_RATIO
        and shared_family_ratio <= SHORT_FINAL_PRESERVE_MAX_SHARED_RATIO
    )
    if final_looks_like_short_alternative:
        return previous

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


def _partial_text(confirmed: str, tentative: str) -> str:
    return " ".join(part for part in (confirmed.strip(), tentative.strip()) if part).strip()


def _prepare_candidate_after_confirmed(confirmed: str, candidate: str) -> tuple[str, str]:
    """Align a fresh rolling hypothesis around already-confirmed context."""
    confirmed_raw_words = confirmed.split()
    candidate_raw_words = candidate.split()
    confirmed_words = _normalized_words(confirmed)
    candidate_words = _normalized_words(candidate)
    if not confirmed_words or not candidate_words:
        return confirmed.strip(), candidate.strip()

    if candidate_words[: len(confirmed_words)] == confirmed_words:
        return confirmed.strip(), " ".join(candidate_raw_words[len(confirmed_words) :]).strip()

    confirmed_at = _contiguous_index(candidate_words, confirmed_words)
    if confirmed_at >= 0:
        # A rolling decode can rewind and recover words immediately before the
        # stable prefix. Prepend those words and keep the post-match tail draft.
        recovered_prefix = candidate_raw_words[:confirmed_at]
        augmented_confirmed = " ".join([*recovered_prefix, *confirmed_raw_words]).strip()
        tail_start = confirmed_at + len(confirmed_words)
        return augmented_confirmed, " ".join(candidate_raw_words[tail_start:]).strip()

    if _contiguous_index(confirmed_words, candidate_words) >= 0:
        return confirmed.strip(), ""

    overlap = _suffix_prefix_speech_overlap(confirmed_words, candidate_words)
    if overlap >= 2:
        return confirmed.strip(), " ".join(candidate_raw_words[overlap:]).strip()
    return confirmed.strip(), candidate.strip()


def _append_confirmed(confirmed: str, addition: str) -> str:
    addition = addition.strip()
    if not addition:
        return confirmed.strip()
    stable = confirmed.strip()
    if not stable:
        return addition

    stable_raw_words = stable.split()
    addition_raw_words = addition.split()
    overlap = _suffix_prefix_speech_overlap(
        _normalized_words(stable),
        _normalized_words(addition),
    )
    if overlap >= len(addition_raw_words):
        return stable
    return " ".join([*stable_raw_words, *addition_raw_words[overlap:]]).strip()


def _stabilize_rolling_partial(
    confirmed: str,
    previous_tentative: str,
    next_text: str,
) -> tuple[str, str]:
    """Promote locally-agreed words and keep revisable words tentative.

    The live model repeatedly decodes an overlapping audio window. Words seen in
    two compatible windows become confirmed. A competing hypothesis replaces
    only the tentative tail; it is never appended solely because it shares the
    first word with the previous hypothesis.
    """
    stable = confirmed.strip()
    previous = previous_tentative.strip()
    stable, candidate = _prepare_candidate_after_confirmed(stable, next_text.strip())

    if not candidate:
        return stable, previous
    if not previous:
        return stable, candidate

    previous_raw_words = previous.split()
    candidate_raw_words = candidate.split()
    previous_words = _normalized_words(previous)
    candidate_words = _normalized_words(candidate)

    if previous_words == candidate_words:
        return stable, previous
    if candidate_words[: len(previous_words)] == previous_words:
        promoted = _append_confirmed(stable, previous)
        return promoted, " ".join(candidate_raw_words[len(previous_words) :]).strip()
    if previous_words[: len(candidate_words)] == candidate_words:
        return stable, previous
    if _contiguous_index(candidate_words, previous_words) >= 0:
        return stable, candidate
    if _contiguous_index(previous_words, candidate_words) >= 0:
        return stable, previous

    overlap = _suffix_prefix_speech_overlap(previous_words, candidate_words)
    if overlap > 0:
        promoted = _append_confirmed(stable, previous)
        return promoted, " ".join(candidate_raw_words[overlap:]).strip()

    common_prefix = _common_prefix_size(previous_words, candidate_words)
    if common_prefix > 0:
        promoted = _append_confirmed(stable, " ".join(previous_raw_words[:common_prefix]))
        return promoted, " ".join(candidate_raw_words[common_prefix:]).strip()

    shared_family_ratio = _shared_token_ratio(
        _overlap_families(previous_words),
        _overlap_families(candidate_words),
    )
    if shared_family_ratio >= 0.5:
        return stable, candidate

    # With no lexical relationship, the rolling window has moved on. Preserve
    # the previous window as stable context and expose the new window as draft.
    return _append_confirmed(stable, previous), candidate


def _select_partial_parts(
    draft: str,
    confirmed: str,
    tentative: str,
) -> tuple[str, str] | None:
    candidate = (draft or "").strip()
    if not candidate or is_hallucination(candidate):
        return None

    selected = _stabilize_rolling_partial(confirmed, tentative, candidate)
    current = (confirmed.strip(), tentative.strip())
    if selected == current:
        return None

    displayed = _partial_text(*selected)
    if not displayed or is_hallucination(displayed):
        return None
    return selected


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


class StreamProtocolError(ValueError):
    """Client violated the bounded live-stream control protocol."""


def _decode_terminal_control(value: str) -> None:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise StreamProtocolError("invalid_client_control") from exc
    if payload != EOF_CONTROL:
        raise StreamProtocolError("invalid_client_control")


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
            "partial_mode": "stable-v1",
            "capabilities": ["eof"],
            "supports_eof": True,
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
    confirmed_draft = ""
    tentative_draft = ""
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

    async def emit_final(payload: dict[str, object]) -> None:
        await send_json(payload)
        await send_debug(
            "final_sent",
            seq=payload["seq"],
            elapsed_ms=payload["elapsed_ms"],
            text_len=len(str(payload["text"])),
        )
        # KVKK: no transcript content in server logs.
        logger.info(
            "Final segment sent",
            extra={
                "seq": payload["seq"],
                "reason": payload["reason"],
                "elapsed_ms": payload["elapsed_ms"],
            },
        )

    async def commit_current(
        reason: str,
        *,
        defer_final: bool = False,
    ) -> dict[str, object] | None:
        nonlocal buffer, buffer_start_t, seg_index, last_draft, confirmed_draft
        nonlocal tentative_draft, speech_seen
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
            nonlocal buffer, buffer_start_t, seg_index, last_draft, confirmed_draft
            nonlocal tentative_draft, speech_seen
            nonlocal last_speech_t, buffer_start_sample

            async with buffer_lock:
                seg_index += 1
                last_draft = ""
                confirmed_draft = ""
                tentative_draft = ""

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
            return None

        rms = _audio_rms(audio)
        if rms < settings.min_speech_rms:
            await send_debug("final_skip_low_rms", rms=round(rms, 5), buffer_sec=buffer_sec)
            await advance_segment(
                retain_tail=False,
                commit_end_sample=commit_end_sample,
                committed_audio=audio,
            )
            return None

        await send_debug("final_start", reason=reason, rms=round(rms, 5), buffer_sec=buffer_sec)
        started = time.perf_counter()
        try:
            text = await run_in_threadpool(
                final_service.transcribe_array,
                audio,
                settings.stream_final_vad_filter,
            )
        except Exception as exc:  # Keep non-terminal streams alive by falling back to draft.
            # exc_info is transcript-free (code paths only) — KVKK-safe diagnostics.
            logger.warning("Final pass error err_class=%s", type(exc).__name__, exc_info=True)
            await send_debug("final_error", error=type(exc).__name__)
            if reason == "eof":
                raise
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
            return None

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
            return None

        final_payload: dict[str, object] = {
            "type": "final",
            "seq": active_seq,
            "text": text,
            "reason": reason,
            "elapsed_ms": elapsed_ms,
            "rms": round(rms, 5),
        }
        if not defer_final:
            await emit_final(final_payload)
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
        return final_payload

    async def infer_live_partial() -> None:
        nonlocal last_draft, confirmed_draft, tentative_draft

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

        selected_parts = _select_partial_parts(draft, confirmed_draft, tentative_draft)
        if selected_parts is None:
            await send_debug(
                "draft_regression_filtered",
                elapsed_ms=elapsed_ms,
                previous_words=_word_count(last_draft),
                candidate_words=_word_count(draft),
            )
            return

        previous_draft = last_draft
        next_confirmed, next_tentative = selected_parts
        selected_draft = _partial_text(next_confirmed, next_tentative)
        last_draft = selected_draft
        confirmed_draft = next_confirmed
        tentative_draft = next_tentative
        if selected_draft == previous_draft:
            await send_debug("draft_stability_advanced", seq=active_seq, elapsed_ms=elapsed_ms)
            return
        await send_json(
            {
                "type": "partial",
                "seq": active_seq,
                "confirmed": confirmed_draft,
                "tentative": tentative_draft,
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

    async def inference_loop() -> None:
        nonlocal last_live_infer_t

        while not stop_inference.is_set():
            await asyncio.sleep(0.05)
            if stop_inference.is_set():
                break
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
    terminal_task: asyncio.Task[dict[str, object] | None] | None = None

    async def finalize_terminal() -> dict[str, object] | None:
        final_payload = await commit_current("eof", defer_final=True)
        # Give the receive task a chance to surface an already-queued post-EOF
        # frame before the server emits the terminal success marker.
        await asyncio.sleep(0)
        return final_payload

    async def reject_post_eof_frame() -> dict[str, object] | None:
        nonlocal terminal_task

        terminal_task = asyncio.create_task(finalize_terminal())
        receive_task = asyncio.create_task(websocket.receive())
        done, _ = await asyncio.wait(
            {terminal_task, receive_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # A client frame or disconnect observed before finalization completes is
        # not allowed to race the terminal boundary. Prefer the protocol failure
        # even when both tasks became ready in the same event-loop turn.
        if receive_task in done:
            message = receive_task.result()
            terminal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await terminal_task
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(code=int(message.get("code", 1000)))
            raise StreamProtocolError("post_eof_frame")

        final_payload = await terminal_task
        receive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receive_task
        return final_payload

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(code=int(message.get("code", 1000)))

            text = message.get("text")
            if text is not None:
                _decode_terminal_control(text)
                # No live partial may cross the acknowledged terminal boundary.
                # A partial already in flight is allowed to finish before the
                # acknowledgement; after eof_ack only late finals and drained
                # are valid server events.
                stop_inference.set()
                await inference_task
                await send_json({"type": "eof_ack"})
                final_payload = await reject_post_eof_frame()
                if final_payload is not None:
                    await emit_final(final_payload)
                await send_json({"type": "drained"})
                await websocket.close(code=1000)
                return

            data = message.get("bytes")
            if data is None:
                raise StreamProtocolError("invalid_client_frame")
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
    except StreamProtocolError as exc:
        logger.warning("WS stream protocol rejected reason=%s", str(exc))
        with contextlib.suppress(Exception):
            await send_json({"type": "error", "msg": str(exc)})
            await websocket.close(code=1003)
    except Exception:
        logger.exception("WS stream error")
        with contextlib.suppress(Exception):
            await websocket.close()
    finally:
        stop_inference.set()
        if terminal_task is not None and not terminal_task.done():
            terminal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await terminal_task
        if not inference_task.done():
            inference_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await inference_task
