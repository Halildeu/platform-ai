#!/usr/bin/env python3
"""Download 2 Common Voice 17 TR sample clips (5-10 sec) - CC0 1.0.

Codex `019e8a24` REVISE absorb: A (Common Voice TR minimum) + license/source manifest.

Usage:
    pip install datasets soundfile
    python scripts/download-cv17-tr-samples.py --out tests/fixtures/

Auth (not usually required for the fallback mirror, but helps avoid rate limits):
    huggingface-cli login
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


DEFAULT_DATASET = "mozilla-foundation/common_voice_17_0"
FALLBACK_DATASET = "fsicoli/common_voice_17_0"


def _load_streaming_dataset(dataset_name: str, locale: str) -> Iterable[dict[str, Any]]:
    from datasets import load_dataset  # type: ignore

    trust_remote_code = dataset_name != DEFAULT_DATASET
    return load_dataset(
        dataset_name,
        locale,
        split="test",
        streaming=True,
        trust_remote_code=trust_remote_code,
    )


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
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Primary HuggingFace dataset repo (default: {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--fallback-dataset",
        default=FALLBACK_DATASET,
        help=f"Fallback HuggingFace dataset repo (default: {FALLBACK_DATASET})",
    )
    args = parser.parse_args()

    try:
        import soundfile as sf  # type: ignore
    except ImportError:
        print("ERROR: pip install datasets soundfile", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    dataset_names = [args.dataset]
    if args.fallback_dataset and args.fallback_dataset not in dataset_names:
        dataset_names.append(args.fallback_dataset)

    selected: list[dict[str, Any]] = []
    used_dataset: str | None = None
    last_error: Exception | None = None

    for dataset_name in dataset_names:
        print(f"Loading Common Voice 17 TR test split from {dataset_name} (streaming)...")
        try:
            ds = _load_streaming_dataset(dataset_name, "tr")
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
                        used_dataset = dataset_name
                        break
            if len(selected) == args.count:
                break
            print(
                f"WARNING: only {len(selected)}/{args.count} samples from {dataset_name}; trying next source",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should fall back across dataset mirrors.
            last_error = exc
            print(f"WARNING: {dataset_name} failed: {exc}", file=sys.stderr)

    if len(selected) < args.count:
        if last_error is not None:
            print(f"ERROR: last dataset error: {last_error}", file=sys.stderr)
        print(
            f"ERROR: only {len(selected)}/{args.count} samples in duration range",
            file=sys.stderr,
        )
        return 1

    for i, ex in enumerate(selected, 1):
        wav_path = args.out / f"sample-tr-cv17-{i:03d}.wav"
        txt_path = args.out / f"sample-tr-cv17-{i:03d}.txt"
        sf.write(wav_path, ex["audio"]["array"], ex["audio"]["sampling_rate"])
        txt_path.write_text(ex["sentence"], encoding="utf-8")
        print(f"  OK {wav_path} ({wav_path.stat().st_size} bytes)")
        print(f"  OK {txt_path}")

    print(f"\nDone. {len(selected)} samples in {args.out}")
    print(f"Source dataset: {used_dataset}")
    print("License: CC0 1.0 - Mozilla Common Voice 17.0 TR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
