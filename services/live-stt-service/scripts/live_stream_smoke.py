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
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import websockets

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

TARGET_SAMPLE_RATE = 16_000
DEFAULT_FRAME_MS = 200
DEFAULT_TAIL_SILENCE_SEC = 1.2
DEFAULT_TIMEOUT_SEC = 90.0


class SmokeError(RuntimeError):
    """Expected smoke failure with a redacted message."""


def _word_count(text: str) -> int:
    return len(text.split())


def _is_hallucination(text: str) -> bool:
    from app.services.hallucination import is_hallucination

    return is_hallucination(text)


def text_digest(text: str, *, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def bytes_digest(content: bytes, *, length: int = 12) -> str:
    return hashlib.sha256(content).hexdigest()[:length]


def load_wav_float32(path: Path, sample_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """Load PCM16 WAV as mono float32 at the target sample rate."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        source_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())

    if channels < 1:
        raise SmokeError(f"{path} has no audio channels")
    if sample_width != 2:
        raise SmokeError(f"{path} must be PCM16 WAV; sample_width={sample_width}")

    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    audio = np.clip(pcm / 32768.0, -1.0, 1.0).astype(np.float32)

    if source_rate == sample_rate:
        return audio

    if source_rate <= 0:
        raise SmokeError(f"{path} has invalid sample rate {source_rate}")
    duration = audio.shape[0] / source_rate
    target_len = max(1, int(round(duration * sample_rate)))
    source_x = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    target_x = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(target_x, source_x, audio).astype(np.float32)


def audio_frames(audio: np.ndarray, *, frame_ms: int) -> list[np.ndarray]:
    frame_samples = max(1, int(TARGET_SAMPLE_RATE * frame_ms / 1000))
    return [
        audio[index : index + frame_samples]
        for index in range(0, audio.shape[0], frame_samples)
    ]


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
    errors: list[str],
) -> dict[str, Any]:
    final_events = [event for event in transcript_events if event["type"] == "final"]
    partial_events = [event for event in transcript_events if event["type"] == "partial"]
    final_hallucination_count = sum(1 for event in final_events if event.get("hallucination_flag"))
    first_partial_at_ms = partial_events[0]["received_at_ms"] if partial_events else None
    first_final_at_ms = final_events[0]["received_at_ms"] if final_events else None
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    ready_at_ms = int((ready_at - started_at) * 1000) if ready_at is not None else None

    return {
        "schema": "platform-ai.live-stt.stream-smoke.v1",
        "ok": bool(ready_at and final_events and final_hallucination_count == 0 and not errors),
        "url": url,
        "fixture": {
            "path": str(wav_path),
            "audio_sha256_12": bytes_digest(wav_path.read_bytes()),
            "duration_ms": int(audio_samples / TARGET_SAMPLE_RATE * 1000),
            "sample_rate": TARGET_SAMPLE_RATE,
        },
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
        },
        "transcript_events_redacted": transcript_events,
        "errors": errors,
        "privacy": {
            "raw_audio_logged": False,
            "transcript_text_logged": False,
            "hashes_only": True,
        },
    }


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    wav_path = Path(args.wav).expanduser().resolve()
    audio = load_wav_float32(wav_path)
    frames = audio_frames(audio, frame_ms=args.frame_ms)
    tail_silence = np.zeros(
        int(TARGET_SAMPLE_RATE * args.tail_silence_sec),
        dtype=np.float32,
    )
    frames.extend(audio_frames(tail_silence, frame_ms=args.frame_ms))

    started_at = time.perf_counter()
    loading_events: list[str] = []
    transcript_events: list[dict[str, Any]] = []
    errors: list[str] = []
    ready_at: float | None = None

    async with websockets.connect(args.url, open_timeout=args.timeout_sec) as websocket:
        while ready_at is None:
            event = json.loads(await asyncio.wait_for(websocket.recv(), args.timeout_sec))
            event_type = event.get("type")
            if event_type == "loading":
                stage = event.get("stage", "-")
                loading_events.append(f"loading:{stage}")
            elif event_type == "ready":
                ready_at = time.perf_counter()
            elif event_type == "error":
                errors.append(str(event.get("msg", "error")))
                break

        if ready_at is not None:
            async def receiver() -> None:
                while True:
                    event = json.loads(await websocket.recv())
                    received_at_ms = int((time.perf_counter() - started_at) * 1000)
                    event_type = event.get("type")
                    if event_type in {"partial", "final"}:
                        transcript_events.append(redacted_transcript_event(event, received_at_ms))
                    elif event_type == "error":
                        errors.append(str(event.get("msg", "error")))

            receiver_task = asyncio.create_task(receiver())
            for frame in frames:
                await websocket.send(frame.astype(np.float32).tobytes())
                await asyncio.sleep(args.frame_ms / 1000)

            deadline = time.perf_counter() + args.final_wait_sec
            while time.perf_counter() < deadline:
                if any(event["type"] == "final" for event in transcript_events):
                    break
                await asyncio.sleep(0.05)
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass

    return build_summary(
        url=args.url,
        wav_path=wav_path,
        audio_samples=int(audio.shape[0]),
        started_at=started_at,
        loading_events=loading_events,
        ready_at=ready_at,
        transcript_events=transcript_events,
        errors=errors,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run transcript-free /ws/stream smoke")
    parser.add_argument("--url", default="ws://127.0.0.1:18220/ws/stream")
    parser.add_argument(
        "--wav",
        default=str(Path(__file__).resolve().parents[1] / "tests/fixtures/sample-tr-cv17-001.wav"),
    )
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    parser.add_argument("--tail-silence-sec", type=float, default=DEFAULT_TAIL_SILENCE_SEC)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--final-wait-sec", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = asyncio.run(run_smoke(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
