"""#161 (T-B) synthetic multi-speaker dataset for diarization DER measurement.

Builds a deterministic multi-speaker conversation by concatenating distinct
single-speaker Turkish clips (Common Voice TR — each clip a different person)
into one WAV, with exact RTTM ground-truth (we KNOW who speaks when because we
placed each turn). This is the "ground-truth kesin" leg for diar_matrix.py —
the diarization twin of make_synthetic_tr.py. Stdlib + numpy only (no ffmpeg).

Usage:
  python scripts/make_synthetic_diar.py --src services/live-stt-service/tests/fixtures \
      --dst tests/fixtures/diar-tr --num-speakers 3 --turns 9 --gap-sec 0.4 --seed 7
Produces <dst>/synthetic-diar-<seed>.wav + .rttm — diar_matrix.py reads them
directly (wav + same-stem .rttm). CI-only synthetic; real meeting leg is pilot.
"""
# ruff: noqa: T201 - CLI tool: prints are the output.

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 16000


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return data, rate


def _write_wav(path: Path, data: np.ndarray, rate: int) -> None:
    clipped = np.clip(data, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(clipped.tobytes())


def _resample_to_16k(data: np.ndarray, src_rate: int) -> np.ndarray:
    """Linear resample to 16 kHz (diarization standard; keeps RTTM durations
    consistent with the audio). Dependency-free; same numpy-interp idea as
    make_synthetic_tr.py's speed perturbation."""
    if src_rate == TARGET_RATE:
        return data
    ratio = src_rate / TARGET_RATE
    idx = np.arange(0, len(data), ratio)
    return np.interp(idx, np.arange(len(data)), data)


def build_conversation(
    clips: list[np.ndarray], rate: int, turns: int, gap_sec: float, seed: int
) -> tuple[np.ndarray, list[tuple[float, float, str]]]:
    """Round-robin speakers with a deterministic shuffle; return (audio, rttm-turns)."""
    rng = np.random.default_rng(seed)
    n_speakers = len(clips)
    order = [i % n_speakers for i in range(turns)]
    rng.shuffle(order)  # deterministic given seed; avoids trivial strict round-robin

    gap = np.zeros(int(gap_sec * rate), dtype=np.float32)
    audio_parts: list[np.ndarray] = []
    rttm: list[tuple[float, float, str]] = []
    t = 0.0
    for spk in order:
        clip = clips[spk]
        dur = len(clip) / rate
        rttm.append((round(t, 3), round(dur, 3), f"SPEAKER_{spk:02d}"))
        audio_parts.append(clip)
        audio_parts.append(gap)
        t += dur + gap_sec
    return np.concatenate(audio_parts) if audio_parts else np.zeros(0, dtype=np.float32), rttm


def write_rttm(path: Path, file_id: str, turns: list[tuple[float, float, str]]) -> None:
    lines = [
        f"SPEAKER {file_id} 1 {start:.3f} {dur:.3f} <NA> <NA> {spk} <NA> <NA>"
        for start, dur, spk in turns
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="dir with single-speaker CV-TR wavs")
    ap.add_argument("--dst", default="tests/fixtures/diar-tr")
    ap.add_argument("--num-speakers", type=int, default=3)
    ap.add_argument("--turns", type=int, default=9)
    ap.add_argument("--gap-sec", type=float, default=0.4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--speaker-offset",
        type=int,
        default=0,
        help="pick wavs[offset:offset+num_speakers] — lets a sweep build distinct "
        "fixtures from different CV-TR voices (more diverse n>1 baseline).",
    )
    args = ap.parse_args()

    src = Path(args.src)
    all_wavs = sorted(src.glob("*.wav"))
    off = max(0, args.speaker_offset)
    wavs = all_wavs[off : off + args.num_speakers]
    if len(wavs) < args.num_speakers:
        print(
            f"need {args.num_speakers} distinct wavs in {src} at offset {off}, "
            f"found {len(wavs)} (total {len(all_wavs)})",
            file=sys.stderr,
        )
        sys.exit(2)

    clips: list[np.ndarray] = []
    for w in wavs:
        data, rate = _read_wav(w)
        if rate != TARGET_RATE:
            data = _resample_to_16k(data, rate)  # → 16 kHz so RTTM ↔ audio match
        clips.append(data)
    rate = TARGET_RATE

    audio, turns = build_conversation(clips, rate, args.turns, args.gap_sec, args.seed)

    dst = Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)
    stem = f"synthetic-diar-{args.seed}"
    _write_wav(dst / f"{stem}.wav", audio, rate)
    write_rttm(dst / f"{stem}.rttm", stem, turns)

    total_sec = len(audio) / rate
    print(
        f"OK {stem}: {args.num_speakers} speakers, {args.turns} turns, "
        f"{total_sec:.1f}s -> {dst}/{stem}.wav + .rttm"
    )


if __name__ == "__main__":
    main()
