#!/usr/bin/env python3
"""Download 2 Common Voice 17 TR sample clips (5-10 sn) — CC0 1.0.

Codex `019e8a24` REVISE absorb: A (Common Voice TR minimum) + license/source manifest.

Usage:
    pip install datasets soundfile
    python scripts/download-cv17-tr-samples.py --out tests/fixtures/

Auth (gated dataset değil ama rate limit avoid):
    huggingface-cli login
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Download CV17 TR samples")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/fixtures"),
        help="Output directory for wav + txt files",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=2,
        help="Number of samples to download (default: 2)",
    )
    parser.add_argument(
        "--min-sec",
        type=float,
        default=5.0,
        help="Minimum duration (seconds, default: 5.0)",
    )
    parser.add_argument(
        "--max-sec",
        type=float,
        default=10.0,
        help="Maximum duration (seconds, default: 10.0)",
    )
    args = parser.parse_args()

    try:
        from datasets import load_dataset  # type: ignore
        import soundfile as sf  # type: ignore
    except ImportError:
        print("ERROR: pip install datasets soundfile", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Loading Common Voice 17 TR test split (streaming)...")
    ds = load_dataset(
        "mozilla-foundation/common_voice_17_0",
        "tr",
        split="test",
        streaming=True,
        trust_remote_code=False,
    )

    selected = []
    for ex in ds:
        audio = ex["audio"]
        sr = audio["sampling_rate"]
        duration = audio["array"].shape[0] / sr
        if args.min_sec <= duration <= args.max_sec:
            selected.append(ex)
            print(
                f"  + sample {len(selected)}: {duration:.1f}s | "
                f"sr={sr} | sentence='{ex['sentence'][:50]}...'"
            )
            if len(selected) == args.count:
                break

    if len(selected) < args.count:
        print(
            f"WARNING: only {len(selected)}/{args.count} samples in duration range",
            file=sys.stderr,
        )

    for i, ex in enumerate(selected, 1):
        wav_path = args.out / f"sample-tr-cv17-{i:03d}.wav"
        txt_path = args.out / f"sample-tr-cv17-{i:03d}.txt"
        sf.write(wav_path, ex["audio"]["array"], ex["audio"]["sampling_rate"])
        txt_path.write_text(ex["sentence"], encoding="utf-8")
        print(f"  ✓ {wav_path} ({wav_path.stat().st_size} bytes)")
        print(f"  ✓ {txt_path}")

    print(f"\nDone. {len(selected)} samples in {args.out}")
    print("License: CC0 1.0 — Mozilla Common Voice 17.0 TR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
