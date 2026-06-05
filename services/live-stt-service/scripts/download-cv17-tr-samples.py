#!/usr/bin/env python3
"""Download Mozilla Common Voice 17 TR sample clips - CC0 1.0.

Codex `019e8a24` REVISE absorb: A (Common Voice TR minimum) + license/source manifest.

Usage:
    pip install datasets soundfile
    python scripts/download-cv17-tr-samples.py --out tests/fixtures/
    python scripts/download-cv17-tr-samples.py --count 150 --selection random \
        --out ../../tests/fixtures/wer-common-voice-tr \
        --manifest-json ../../tests/fixtures/wer-common-voice-tr/ground-truth.json

Auth (not usually required for the fallback mirror, but helps avoid rate limits):
    huggingface-cli login
"""

from __future__ import annotations

# ruff: noqa: T201
import argparse
import json
import random
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DATASET = "mozilla-foundation/common_voice_17_0"
FALLBACK_DATASET = "fsicoli/common_voice_17_0"
COMMON_VOICE_SOURCE_URL = "https://commonvoice.mozilla.org/tr/datasets"
COMMON_VOICE_LICENSE = "CC0 1.0"


def _load_streaming_dataset(dataset_name: str, locale: str, split: str) -> Iterable[dict[str, Any]]:
    from datasets import load_dataset  # type: ignore

    trust_remote_code = dataset_name != DEFAULT_DATASET
    return load_dataset(
        dataset_name,
        locale,
        split=split,
        streaming=True,
        trust_remote_code=trust_remote_code,
    )


def _duration_sec(example: dict[str, Any]) -> float:
    audio = example["audio"]
    return float(audio["array"].shape[0] / audio["sampling_rate"])


def _select_first(
    dataset: Iterable[dict[str, Any]],
    count: int,
    min_sec: float,
    max_sec: float,
    scan_limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for scanned, ex in enumerate(dataset, 1):
        if scanned > scan_limit:
            break
        duration = _duration_sec(ex)
        if min_sec <= duration <= max_sec:
            selected.append(ex)
            print(
                f"  + sample {len(selected)}: {duration:.1f}s | "
                f"sr={ex['audio']['sampling_rate']} | sentence='{ex['sentence'][:50]}...'"
            )
            if len(selected) == count:
                break
    return selected


def _select_random_reservoir(
    dataset: Iterable[dict[str, Any]],
    count: int,
    min_sec: float,
    max_sec: float,
    scan_limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture sampling, not security.
    reservoir: list[dict[str, Any]] = []
    eligible = 0
    scanned = 0
    for scanned, ex in enumerate(dataset, 1):
        if scanned > scan_limit:
            break
        duration = _duration_sec(ex)
        if not min_sec <= duration <= max_sec:
            continue
        eligible += 1
        if len(reservoir) < count:
            reservoir.append(ex)
        else:
            replace_at = rng.randint(0, eligible - 1)
            if replace_at < count:
                reservoir[replace_at] = ex
    print(
        f"  scanned={min(scanned, scan_limit)} eligible={eligible} "
        f"selected={len(reservoir)} seed={seed}"
    )
    return reservoir


def _write_manifest(
    manifest_path: Path,
    selected: list[dict[str, Any]],
    dataset_name: str,
    split: str,
    locale: str,
    output_dir: Path,
    prefix: str,
) -> None:
    samples: list[dict[str, Any]] = []
    for i, ex in enumerate(selected, 1):
        wav_name = f"{prefix}-{i:03d}.wav"
        txt_name = f"{prefix}-{i:03d}.txt"
        samples.append(
            {
                "id": f"{prefix}-{i:03d}",
                "wav": wav_name,
                "transcript_txt": txt_name,
                "sentence": ex["sentence"],
                "duration_sec": round(_duration_sec(ex), 3),
                "sample_rate_hz": ex["audio"]["sampling_rate"],
                "locale": locale,
                "split": split,
                "source_dataset": dataset_name,
                "license": COMMON_VOICE_LICENSE,
            }
        )

    manifest = {
        "dataset": "Mozilla Common Voice 17.0 Turkish",
        "source_dataset": dataset_name,
        "source_url": COMMON_VOICE_SOURCE_URL,
        "license": COMMON_VOICE_LICENSE,
        "locale": locale,
        "split": split,
        "sample_count": len(samples),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # noqa: UP017
        "output_dir": str(output_dir),
        "pii_policy": (
            "No Common Voice client_id, speaker id, email, or raw user metadata is stored."
        ),
        "samples": samples,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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
        help="Number of samples to download (default: 2; WER PoC usually 100-200)",
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
    parser.add_argument(
        "--split",
        default="test",
        help="Common Voice split to read (default: test)",
    )
    parser.add_argument(
        "--locale",
        default="tr",
        help="Common Voice locale/config to read (default: tr)",
    )
    parser.add_argument(
        "--selection",
        choices=("first", "random"),
        default="first",
        help="Selection mode. 'first' preserves smoke fixture behavior; 'random' is for WER PoC.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260605,
        help="Random selection seed when --selection=random",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=5000,
        help="Maximum streaming examples to scan before failing (default: 5000)",
    )
    parser.add_argument(
        "--prefix",
        default="sample-tr-cv17",
        help="Output filename prefix (default: sample-tr-cv17)",
    )
    parser.add_argument(
        "--manifest-json",
        type=Path,
        default=None,
        help="Optional ground truth manifest JSON path",
    )
    args = parser.parse_args()

    if args.count <= 0:
        print("ERROR: --count must be positive", file=sys.stderr)
        return 2
    if args.scan_limit < args.count:
        print("ERROR: --scan-limit must be >= --count", file=sys.stderr)
        return 2

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
        print(
            f"Loading Common Voice 17 {args.locale} {args.split} split "
            f"from {dataset_name} (streaming)..."
        )
        try:
            ds = _load_streaming_dataset(dataset_name, args.locale, args.split)
            if args.selection == "first":
                selected = _select_first(
                    ds,
                    args.count,
                    args.min_sec,
                    args.max_sec,
                    args.scan_limit,
                )
            else:
                selected = _select_random_reservoir(
                    ds,
                    args.count,
                    args.min_sec,
                    args.max_sec,
                    args.scan_limit,
                    args.seed,
                )
            if len(selected) == args.count:
                used_dataset = dataset_name
                break
            print(
                f"WARNING: only {len(selected)}/{args.count} samples from "
                f"{dataset_name}; trying next source",
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
        wav_path = args.out / f"{args.prefix}-{i:03d}.wav"
        txt_path = args.out / f"{args.prefix}-{i:03d}.txt"
        sf.write(wav_path, ex["audio"]["array"], ex["audio"]["sampling_rate"])
        txt_path.write_text(ex["sentence"], encoding="utf-8")
        print(f"  OK {wav_path} ({wav_path.stat().st_size} bytes)")
        print(f"  OK {txt_path}")

    if args.manifest_json is not None:
        _write_manifest(
            args.manifest_json,
            selected,
            used_dataset or args.dataset,
            args.split,
            args.locale,
            args.out,
            args.prefix,
        )
        print(f"  OK {args.manifest_json}")

    print(f"\nDone. {len(selected)} samples in {args.out}")
    print(f"Source dataset: {used_dataset}")
    print(f"License: {COMMON_VOICE_LICENSE} - Mozilla Common Voice 17.0 TR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
