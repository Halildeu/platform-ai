"""#35 synthetic-condition dataset: degrade clean CV samples (noise + speed).

Triangulation leg 2: the same Turkish references under controlled degradation
(meeting-room-like conditions) — additive white noise at a target SNR and a
speed perturbation. Stdlib + numpy only.

Usage:
  python scripts/make_synthetic_tr.py --src tests/fixtures/wer-cv17-tr \
      --dst tests/fixtures/wer-synthetic-tr --snr-db 10 --speed 1.1
Copies `<stem>.txt` references alongside so wer_matrix.py works unchanged.
"""
# ruff: noqa: T201 - CLI tool: prints are the output.

from __future__ import annotations

import argparse
import shutil
import sys
import wave
from pathlib import Path

import numpy as np


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return data, rate


def _write_wav(path: Path, data: np.ndarray, rate: int) -> None:
    clipped = np.clip(data, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(clipped.tobytes())


def degrade(data: np.ndarray, rate: int, snr_db: float, speed: float, seed: int) -> np.ndarray:
    # Speed perturbation via linear resample (duration / pitch shift together —
    # crude but deterministic and dependency-free).
    if speed != 1.0:
        idx = np.arange(0, len(data), speed)
        data = np.interp(idx, np.arange(len(data)), data)
    # Additive white noise at target SNR.
    rng = np.random.default_rng(seed)
    sig_power = float(np.mean(data**2)) or 1.0
    noise_power = sig_power / (10 ** (snr_db / 10))
    noise = rng.normal(0.0, noise_power**0.5, size=len(data))
    return data + noise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="tests/fixtures/wer-cv17-tr")
    ap.add_argument("--dst", default="tests/fixtures/wer-synthetic-tr")
    ap.add_argument("--snr-db", type=float, default=10.0)
    ap.add_argument("--speed", type=float, default=1.1)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)
    wavs = sorted(src.glob("*.wav"))
    if args.limit > 0:
        wavs = wavs[: args.limit]
    if not wavs:
        print(f"no wavs in {src}", file=sys.stderr)
        sys.exit(2)

    made = 0
    for wav_path in wavs:
        ref = wav_path.with_suffix(".txt")
        if not ref.exists():
            continue
        data, rate = _read_wav(wav_path)
        out = degrade(data, rate, args.snr_db, args.speed, seed=hash(wav_path.name) % 2**32)
        _write_wav(dst / wav_path.name, out, rate)
        shutil.copy2(ref, dst / ref.name)
        made += 1
    print(f"synthetic set: {made} samples -> {dst} (SNR {args.snr_db} dB, speed x{args.speed})")


if __name__ == "__main__":
    main()
