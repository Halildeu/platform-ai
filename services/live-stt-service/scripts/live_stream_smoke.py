"""Transcript-free live /ws/stream smoke client.

Streams a privacy-safe WAV fixture into the direct live STT websocket and emits
only redacted metrics: event counts, latency, text length, word count and short
hashes. It never prints raw transcript text.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
import wave
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlparse, urlunsplit

import numpy as np
import websockets
from numpy.typing import NDArray
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

TARGET_SAMPLE_RATE = 16_000
DEFAULT_FRAME_MS = 200
DEFAULT_TAIL_SILENCE_SEC = 1.2
DEFAULT_TIMEOUT_SEC = 90.0
DEFAULT_FINAL_WAIT_SEC = 90.0
MAX_CLOSE_TIMEOUT_SEC = 5.0
DEFAULT_MIN_FINAL_WORD_COVERAGE = 0.8
DEFAULT_MIN_REFERENCE_TOKEN_COVERAGE = 0.8
DEFAULT_MAX_WORD_ERROR_RATE = 0.25
DEFAULT_MIN_PARTIAL_EVENTS = 1
DEFAULT_MIN_FINAL_EVENTS = 1
DEFAULT_MAX_TRANSCRIPT_GAP_MS = 6000
STREAM_PROTOCOL = "source-ranges-v1"
READY_CAPABILITIES = ["eof", STREAM_PROTOCOL]
PARTIAL_EVENT_KEYS = {
    "type",
    "seq",
    "confirmed",
    "tentative",
    "elapsed_ms",
    "rms",
    "source",
}
FINAL_EVENT_KEYS = {
    "type",
    "seq",
    "text",
    "reason",
    "elapsed_ms",
    "rms",
    "source_start_sample",
    "source_end_sample",
}
FINAL_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
AudioArray = NDArray[np.float32]


class SmokeError(RuntimeError):
    """Expected smoke failure with a redacted message."""


def validate_stream_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise SmokeError("stream URL must be an absolute ws/wss URL")
    if parsed.username is not None or parsed.password is not None:
        raise SmokeError("stream URL must not contain userinfo")
    if parse_qsl(parsed.query, keep_blank_values=True) != [("protocol", STREAM_PROTOCOL)]:
        raise SmokeError(f"stream URL must negotiate protocol={STREAM_PROTOCOL}")


def redacted_stream_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def validate_ready_event(event: dict[str, Any]) -> None:
    expected_terminal_timeout_ms = event.get("terminal_timeout_ms")
    if (
        event.get("type") != "ready"
        or event.get("sample_rate") != TARGET_SAMPLE_RATE
        or event.get("partial_mode") != "stable-v1"
        or event.get("protocol") != STREAM_PROTOCOL
        or event.get("capabilities") != READY_CAPABILITIES
        or event.get("supports_eof") is not True
        or not isinstance(event.get("live_model"), str)
        or not event.get("live_model")
        or not isinstance(event.get("final_model"), str)
        or not event.get("final_model")
        or isinstance(expected_terminal_timeout_ms, bool)
        or not isinstance(expected_terminal_timeout_ms, int)
        or not 1_000 <= expected_terminal_timeout_ms <= 120_000
    ):
        raise SmokeError("ready event does not satisfy source-ranges-v1 contract")


def _is_non_negative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_non_negative_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def validate_transcript_event(
    event: dict[str, Any],
    *,
    cumulative_samples_sent: int,
    previous_final_seq: int | None,
    previous_final_source_end: int | None = None,
) -> None:
    event_type = event.get("type")
    if event_type == "partial":
        partial_text = f"{event.get('confirmed', '')} {event.get('tentative', '')}".strip()
        valid = (
            set(event) == PARTIAL_EVENT_KEYS
            and _is_non_negative_int(event.get("seq"))
            and isinstance(event.get("confirmed"), str)
            and isinstance(event.get("tentative"), str)
            and bool(_normalized_words(partial_text))
            and not _is_hallucination(partial_text)
            and _is_non_negative_int(event.get("elapsed_ms"))
            and _is_non_negative_number(event.get("rms"))
            and isinstance(event.get("source"), str)
            and bool(event.get("source"))
        )
    elif event_type == "final":
        sequence = event.get("seq")
        source_start = event.get("source_start_sample")
        source_end = event.get("source_end_sample")
        reason = event.get("reason")
        valid = (
            set(event) == FINAL_EVENT_KEYS
            and _is_non_negative_int(sequence)
            and (previous_final_seq is None or int(cast(int, sequence)) > previous_final_seq)
            and isinstance(event.get("text"), str)
            and bool(str(event.get("text", "")).strip())
            and isinstance(reason, str)
            and FINAL_REASON_RE.fullmatch(reason) is not None
            and _is_non_negative_int(event.get("elapsed_ms"))
            and _is_non_negative_number(event.get("rms"))
            and _is_non_negative_int(source_start)
            and _is_non_negative_int(source_end)
            and int(cast(int, source_end)) > int(cast(int, source_start))
            and (
                previous_final_source_end is None
                or int(cast(int, source_end)) > previous_final_source_end
            )
            and int(cast(int, source_end)) <= cumulative_samples_sent
        )
    else:
        valid = False

    if not valid:
        raise SmokeError(f"{event_type or 'unknown'} event violates source-ranges-v1 contract")


def _word_count(text: str) -> int:
    return len(_normalized_words(text))


def _safe_ratio(numerator: int, denominator: int | None) -> float | None:
    if denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _normalized_words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.casefold()).replace("i\u0307", "i")
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9çğıöşü]+", without_marks)


def _edit_distance(reference: list[str], candidate: list[str]) -> int:
    previous = list(range(len(candidate) + 1))
    for ref_index, ref_word in enumerate(reference, start=1):
        current = [ref_index]
        for candidate_index, candidate_word in enumerate(candidate, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[candidate_index] + 1,
                    previous[candidate_index - 1] + (ref_word != candidate_word),
                )
            )
        previous = current
    return previous[-1]


def _lcs_length(reference: list[str], candidate: list[str]) -> int:
    previous = [0] * (len(candidate) + 1)
    for ref_word in reference:
        current = [0]
        for candidate_index, candidate_word in enumerate(candidate, start=1):
            if ref_word == candidate_word:
                current.append(previous[candidate_index - 1] + 1)
            else:
                current.append(max(previous[candidate_index], current[-1]))
        previous = current
    return previous[-1]


def reference_transcript_quality(
    reference_path: Path | None,
    transcript: str | None,
    *,
    repeat: int = 1,
) -> dict[str, Any]:
    """Return content-based scores without retaining transcript or reference text.

    `repeat` mirrors --repeat-audio: when the fixture audio is tiled, the
    expected transcript is the same sentence that many times, so the reference
    token sequence is tiled with it and coverage/WER keep their meaning.
    """
    if reference_path is None or transcript is None:
        return {"reference_token_coverage": None, "word_error_rate": None}
    reference_words = _normalized_words(reference_path.read_text(encoding="utf-8")) * repeat
    candidate_words = _normalized_words(transcript)
    if not reference_words:
        return {"reference_token_coverage": None, "word_error_rate": None}
    return {
        "reference_token_coverage": round(
            _lcs_length(reference_words, candidate_words) / len(reference_words),
            3,
        ),
        "word_error_rate": round(
            _edit_distance(reference_words, candidate_words) / len(reference_words),
            3,
        ),
    }


def _max_event_gap_ms(events: list[dict[str, Any]]) -> int | None:
    ordered = sorted(int(event["received_at_ms"]) for event in events if "received_at_ms" in event)
    if len(ordered) < 2:
        return None
    return max(current - previous for previous, current in pairwise(ordered))


def _is_hallucination(text: str) -> bool:
    from app.services.hallucination import is_hallucination

    return is_hallucination(text)


def text_digest(text: str, *, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def bytes_digest(content: bytes, *, length: int = 12) -> str:
    return hashlib.sha256(content).hexdigest()[:length]


def load_wav_float32(path: Path, sample_rate: int = TARGET_SAMPLE_RATE) -> AudioArray:
    """Load PCM16 WAV as mono float32 at the target sample rate."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        source_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())

    if channels < 1:
        raise SmokeError("audio fixture has no channels")
    if sample_width != 2:
        raise SmokeError("audio fixture must be PCM16 WAV")

    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    audio = cast(AudioArray, np.clip(pcm / 32768.0, -1.0, 1.0).astype(np.float32))

    if source_rate == sample_rate:
        return audio

    if source_rate <= 0:
        raise SmokeError("audio fixture has an invalid sample rate")
    duration = audio.shape[0] / source_rate
    target_len = max(1, int(round(duration * sample_rate)))
    source_x = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    target_x = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return cast(AudioArray, np.interp(target_x, source_x, audio).astype(np.float32))


def audio_frames(audio: AudioArray, *, frame_ms: int) -> list[AudioArray]:
    frame_samples = max(1, int(TARGET_SAMPLE_RATE * frame_ms / 1000))
    return [
        audio[index : index + frame_samples] for index in range(0, audio.shape[0], frame_samples)
    ]


def resolve_reference_text(wav_path: Path, value: str | None) -> Path | None:
    if value:
        path = Path(value).expanduser().resolve()
        return path if path.exists() else None

    candidate = wav_path.with_suffix(".txt")
    return candidate if candidate.exists() else None


def reference_metadata(path: Path | None, *, repeat: int = 1) -> dict[str, Any]:
    if path is None:
        return {"artifact_id_sha256_12": None, "text_sha256_12": None, "words": None}

    text = path.read_text(encoding="utf-8").strip()
    return {
        "artifact_id_sha256_12": text_digest(path.name),
        "text_sha256_12": text_digest(text),
        # Digests stay those of the single source sentence; only the expected
        # word count scales with --repeat-audio.
        "words": _word_count(text) * repeat,
    }


def redacted_transcript_event(event: dict[str, Any], received_at_ms: int) -> dict[str, Any]:
    event_type = str(event.get("type", "unknown"))
    if event_type == "partial":
        text = f"{event.get('confirmed', '')} {event.get('tentative', '')}".strip()
    elif event_type == "final":
        text = str(event.get("text", ""))
    else:
        text = ""

    result: dict[str, Any] = {
        "type": event_type,
        "received_at_ms": received_at_ms,
    }
    if "seq" in event:
        result["seq"] = event.get("seq")
    if "elapsed_ms" in event:
        result["elapsed_ms"] = event.get("elapsed_ms")
    if "rms" in event:
        result["rms"] = event.get("rms")
    if event_type == "final":
        result["source_start_sample"] = event.get("source_start_sample")
        result["source_end_sample"] = event.get("source_end_sample")
    if text:
        result.update(
            {
                "text_sha256_12": text_digest(text),
                "text_chars": len(text),
                "text_words": _word_count(text),
                "hallucination_flag": _is_hallucination(text),
            }
        )
    return result


def build_summary(
    *,
    url: str,
    wav_path: Path,
    audio_samples: int,
    started_at: float,
    loading_events: list[str],
    ready_at: float | None,
    transcript_events: list[dict[str, Any]],
    terminal_events: list[str],
    errors: list[str],
    reference_text_path: Path | None = None,
    min_final_word_coverage: float = DEFAULT_MIN_FINAL_WORD_COVERAGE,
    min_partial_events: int = DEFAULT_MIN_PARTIAL_EVENTS,
    min_final_events: int = DEFAULT_MIN_FINAL_EVENTS,
    max_transcript_gap_ms: int | None = DEFAULT_MAX_TRANSCRIPT_GAP_MS,
    streamed_samples: int | None = None,
    final_transcript_text: str | None = None,
    min_reference_token_coverage: float = DEFAULT_MIN_REFERENCE_TOKEN_COVERAGE,
    max_word_error_rate: float = DEFAULT_MAX_WORD_ERROR_RATE,
    repeat_audio: int = 1,
) -> dict[str, Any]:
    final_events = [event for event in transcript_events if event["type"] == "final"]
    partial_events = [event for event in transcript_events if event["type"] == "partial"]
    final_hallucination_count = sum(1 for event in final_events if event.get("hallucination_flag"))
    final_word_count = sum(int(event.get("text_words", 0)) for event in final_events)
    reference = reference_metadata(reference_text_path, repeat=repeat_audio)
    reference_words = reference["words"] if isinstance(reference["words"], int) else None
    final_word_coverage = _safe_ratio(final_word_count, reference_words)
    content_quality = reference_transcript_quality(
        reference_text_path, final_transcript_text, repeat=repeat_audio
    )
    reference_token_coverage = content_quality["reference_token_coverage"]
    word_error_rate = content_quality["word_error_rate"]
    max_gap_ms = _max_event_gap_ms(transcript_events)
    first_partial_at_ms = partial_events[0]["received_at_ms"] if partial_events else None
    first_final_at_ms = final_events[0]["received_at_ms"] if final_events else None
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    ready_at_ms = int((ready_at - started_at) * 1000) if ready_at is not None else None

    failures: list[str] = []
    if ready_at is None:
        failures.append("ready_missing")
    if errors:
        failures.append("error_events_present")
    if terminal_events != ["eof_ack", "drained"]:
        failures.append("terminal_sequence_invalid")
    if len(final_events) < min_final_events:
        failures.append("final_event_count_below_min")
    if len(partial_events) < min_partial_events:
        failures.append("partial_event_count_below_min")
    if final_hallucination_count:
        failures.append("final_hallucination_detected")
    if final_word_coverage is not None and final_word_coverage < min_final_word_coverage:
        failures.append("final_word_coverage_below_min")
    if reference_words is not None and reference_token_coverage is None:
        failures.append("reference_quality_unavailable")
    elif (
        reference_token_coverage is not None
        and reference_token_coverage < min_reference_token_coverage
    ):
        failures.append("reference_token_coverage_below_min")
    if word_error_rate is not None and word_error_rate > max_word_error_rate:
        failures.append("word_error_rate_above_max")
    if (
        max_transcript_gap_ms is not None
        and max_gap_ms is not None
        and max_gap_ms > max_transcript_gap_ms
    ):
        failures.append("transcript_event_gap_above_max")

    effective_streamed_samples = audio_samples if streamed_samples is None else streamed_samples

    return {
        "schema": "platform-ai.live-stt.stream-smoke.v1",
        "ok": not failures,
        "url": redacted_stream_url(url),
        "fixture": {
            "artifact_id_sha256_12": text_digest(wav_path.name),
            "audio_sha256_12": bytes_digest(wav_path.read_bytes()),
            "duration_ms": int(audio_samples / TARGET_SAMPLE_RATE * 1000),
            "sample_rate": TARGET_SAMPLE_RATE,
            "repeat_audio": repeat_audio,
            "streamed_samples": effective_streamed_samples,
            "streamed_duration_ms": int(effective_streamed_samples / TARGET_SAMPLE_RATE * 1000),
        },
        "reference": reference,
        "latency": {
            "ready_ms": ready_at_ms,
            "first_partial_ms": first_partial_at_ms,
            "first_final_ms": first_final_at_ms,
            "elapsed_ms": elapsed_ms,
        },
        "events": {
            "loading": loading_events,
            "partial_count": len(partial_events),
            "final_count": len(final_events),
            "final_hallucination_count": final_hallucination_count,
            "error_count": len(errors),
            "max_transcript_gap_ms": max_gap_ms,
            "terminal_sequence": terminal_events,
        },
        "coverage": {
            "final_words": final_word_count,
            "reference_words": reference_words,
            "final_word_coverage": final_word_coverage,
            "reference_token_coverage": reference_token_coverage,
            "word_error_rate": word_error_rate,
        },
        "quality_gate": {
            "min_final_events": min_final_events,
            "min_partial_events": min_partial_events,
            "min_final_word_coverage": min_final_word_coverage,
            "min_reference_token_coverage": min_reference_token_coverage,
            "max_word_error_rate": max_word_error_rate,
            "max_transcript_gap_ms": max_transcript_gap_ms,
            "failures": failures,
        },
        "transcript_events_redacted": transcript_events,
        "errors": errors,
        "privacy": {
            "raw_audio_logged": False,
            "transcript_text_logged": False,
            "hashes_only": True,
        },
    }


def final_event_count(transcript_events: list[dict[str, Any]]) -> int:
    return sum(1 for event in transcript_events if event.get("type") == "final")


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    validate_stream_url(args.url)
    wav_path = Path(args.wav).expanduser().resolve()
    reference_text_path = resolve_reference_text(wav_path, args.reference_text)
    if args.repeat_audio < 1:
        raise SmokeError("repeat-audio must be at least 1")
    audio = load_wav_float32(wav_path)
    if args.repeat_audio > 1:
        audio = np.tile(audio, args.repeat_audio)
    frames = audio_frames(audio, frame_ms=args.frame_ms)
    tail_silence = np.zeros(
        int(TARGET_SAMPLE_RATE * args.tail_silence_sec),
        dtype=np.float32,
    )
    frames.extend(audio_frames(tail_silence, frame_ms=args.frame_ms))

    started_at = time.perf_counter()
    loading_events: list[str] = []
    transcript_events: list[dict[str, Any]] = []
    terminal_events: list[str] = []
    errors: list[str] = []
    final_transcript_parts: list[str] = []
    ready_at: float | None = None
    samples_sent = 0
    last_final_seq: int | None = None
    last_final_source_end: int | None = None
    eof_sent = asyncio.Event()

    close_timeout_sec = max(0.1, min(MAX_CLOSE_TIMEOUT_SEC, args.timeout_sec))
    async with websockets.connect(
        args.url,
        open_timeout=args.timeout_sec,
        close_timeout=close_timeout_sec,
    ) as websocket:
        while ready_at is None:
            event = json.loads(await asyncio.wait_for(websocket.recv(), args.timeout_sec))
            event_type = event.get("type")
            if event_type == "loading":
                stage = event.get("stage", "-")
                if stage not in {"live_model", "final_model"}:
                    raise SmokeError("loading event has an invalid stage")
                loading_events.append(f"loading:{stage}")
            elif event_type == "ready":
                validate_ready_event(event)
                ready_at = time.perf_counter()
            elif event_type == "error":
                errors.append("upstream_error")
                break

        if ready_at is not None:

            async def receiver() -> None:
                nonlocal last_final_seq, last_final_source_end
                terminal_state = "streaming"

                while True:
                    try:
                        event = json.loads(await websocket.recv())
                    except ConnectionClosedOK as exc:
                        raise SmokeError("stream closed before drained") from exc
                    except ConnectionClosed as exc:
                        raise SmokeError("stream closed uncleanly") from exc
                    received_at_ms = int((time.perf_counter() - started_at) * 1000)
                    event_type = event.get("type")
                    if event_type in {"partial", "final"}:
                        if terminal_state == "acked" and event_type == "partial":
                            raise SmokeError("partial event received after eof_ack")
                        validate_transcript_event(
                            event,
                            cumulative_samples_sent=samples_sent,
                            previous_final_seq=last_final_seq,
                            previous_final_source_end=last_final_source_end,
                        )
                        transcript_events.append(redacted_transcript_event(event, received_at_ms))
                        if event_type == "final":
                            final_transcript_parts.append(str(event["text"]))
                            last_final_seq = int(cast(int, event["seq"]))
                            last_final_source_end = int(cast(int, event["source_end_sample"]))
                    elif event_type == "eof_ack":
                        if not eof_sent.is_set() or terminal_state != "streaming":
                            raise SmokeError("eof_ack is not caused by local eof")
                        terminal_state = "acked"
                        terminal_events.append("eof_ack")
                    elif event_type == "drained":
                        if not eof_sent.is_set() or terminal_state != "acked":
                            raise SmokeError("drained received before valid eof_ack")
                        terminal_state = "drained"
                        terminal_events.append("drained")
                        try:
                            trailing = await websocket.recv()
                        except ConnectionClosedOK as exc:
                            if exc.rcvd is None or exc.rcvd.code != 1000:
                                raise SmokeError(
                                    "stream did not close with code 1000 after drained"
                                ) from exc
                            return
                        except ConnectionClosed as exc:
                            raise SmokeError("stream closed uncleanly after drained") from exc
                        raise SmokeError(
                            f"trailing event received after drained: {type(trailing).__name__}"
                        )
                    elif event_type == "error":
                        errors.append("upstream_error")
                        return
                    else:
                        raise SmokeError("unexpected event type in stream state machine")

            receiver_task = asyncio.create_task(receiver())
            try:
                for frame in frames:
                    frame_samples = int(frame.shape[0])
                    samples_sent += frame_samples
                    try:
                        await asyncio.wait_for(
                            websocket.send(frame.astype(np.float32).tobytes()),
                            timeout=args.timeout_sec,
                        )
                    except Exception:
                        samples_sent -= frame_samples
                        raise
                    await asyncio.sleep(args.frame_ms / 1000)

                try:
                    eof_sent.set()
                    try:
                        await asyncio.wait_for(
                            websocket.send(
                                json.dumps({"type": "eof"}, separators=(",", ":"))
                            ),
                            timeout=args.timeout_sec,
                        )
                    except Exception:
                        eof_sent.clear()
                        raise
                    await asyncio.wait_for(receiver_task, timeout=args.final_wait_sec)
                except TimeoutError:
                    errors.append("terminal_drain_timeout")
            finally:
                if not receiver_task.done():
                    receiver_task.cancel()
                done, pending = await asyncio.wait(
                    {receiver_task},
                    timeout=close_timeout_sec,
                )
                if pending:
                    transport = getattr(websocket, "transport", None)
                    if transport is not None:
                        transport.abort()
                    raise SmokeError("stream receiver cancellation timed out")
                for task in done:
                    if not task.cancelled():
                        task.exception()

    return build_summary(
        url=args.url,
        wav_path=wav_path,
        audio_samples=int(audio.shape[0]),
        started_at=started_at,
        loading_events=loading_events,
        ready_at=ready_at,
        transcript_events=transcript_events,
        terminal_events=terminal_events,
        errors=errors,
        reference_text_path=reference_text_path,
        min_final_word_coverage=args.min_final_word_coverage,
        min_partial_events=args.min_partial_events,
        min_final_events=args.min_final_events,
        max_transcript_gap_ms=(
            args.max_transcript_gap_ms if args.max_transcript_gap_ms > 0 else None
        ),
        streamed_samples=samples_sent,
        repeat_audio=args.repeat_audio,
        final_transcript_text=" ".join(final_transcript_parts),
        min_reference_token_coverage=args.min_reference_token_coverage,
        max_word_error_rate=args.max_word_error_rate,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run transcript-free /ws/stream smoke")
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:18220/ws/stream?protocol=source-ranges-v1",
    )
    parser.add_argument(
        "--wav",
        default=str(Path(__file__).resolve().parents[1] / "tests/fixtures/sample-tr-cv17-001.wav"),
    )
    parser.add_argument(
        "--repeat-audio",
        type=int,
        default=1,
        help=(
            "Stream the fixture this many times back to back. The draft pass "
            "only emits once the decoder is confident, so a single short clip "
            "gives it too few chances to be a fair gate; the reference token "
            "sequence is tiled with the audio so coverage/WER keep meaning."
        ),
    )
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    parser.add_argument("--tail-silence-sec", type=float, default=DEFAULT_TAIL_SILENCE_SEC)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--final-wait-sec", type=float, default=DEFAULT_FINAL_WAIT_SEC)
    parser.add_argument(
        "--reference-text",
        default=None,
        help="Optional reference TXT; defaults to sibling .txt when present. Text is hashed only.",
    )
    parser.add_argument(
        "--min-final-word-coverage",
        type=float,
        default=DEFAULT_MIN_FINAL_WORD_COVERAGE,
    )
    parser.add_argument("--min-partial-events", type=int, default=DEFAULT_MIN_PARTIAL_EVENTS)
    parser.add_argument("--min-final-events", type=int, default=DEFAULT_MIN_FINAL_EVENTS)
    parser.add_argument(
        "--min-reference-token-coverage",
        type=float,
        default=DEFAULT_MIN_REFERENCE_TOKEN_COVERAGE,
    )
    parser.add_argument(
        "--max-word-error-rate",
        type=float,
        default=DEFAULT_MAX_WORD_ERROR_RATE,
    )
    parser.add_argument(
        "--max-transcript-gap-ms",
        type=int,
        default=DEFAULT_MAX_TRANSCRIPT_GAP_MS,
        help="Max allowed gap between transcript events; <=0 disables the check.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = asyncio.run(run_smoke(args))
    except SmokeError:
        print(
            json.dumps(
                {
                    "schema": "platform-ai.live-stt.stream-smoke.error.v1",
                    "ok": False,
                    "error_code": "smoke_contract_failed",
                },
                separators=(",", ":"),
            )
        )
        return 1
    except Exception:  # noqa: BLE001 - CLI boundary must never emit raw traceback/evidence
        print(
            json.dumps(
                {
                    "schema": "platform-ai.live-stt.stream-smoke.error.v1",
                    "ok": False,
                    "error_code": "smoke_internal_failed",
                },
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
