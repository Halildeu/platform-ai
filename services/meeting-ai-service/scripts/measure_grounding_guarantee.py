#!/usr/bin/env python3
"""gitops#3444 — measure the extractive grounding guarantee on REAL transcripts.

The claim "a selected sentence always grounds" is only as good as the last time
someone checked it against production text. Synthetic unit tests pin the known
failure shapes; this script re-runs the guarantee over whatever the cluster
actually holds, so a future change to the splitter, the token filter, the
polarity table or the number gate cannot quietly downgrade it from a guarantee
back to a tendency.

Run it after any change to `citation.py` / `extractive.py`, and after a deploy:

    python scripts/measure_grounding_guarantee.py transcripts.json

Input: a JSON list of objects with a "transcript" key (extra keys ignored) —
e.g. dumped from transcript_service.transcript_segments. Transcript text is
read, never printed: only counts and a bounded, redaction-safe reason table
reach stdout.

Exit code is 1 when any selectable sentence fails to ground, so the script can
gate a release without a human reading its output.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.citation import ground_claim, split_sentences  # noqa: E402
from app.services.extractive import materialize_selection, selectable_sentences  # noqa: E402


def measure(transcripts: list[str]) -> tuple[int, int, collections.Counter[str], int]:
    total = grounded = 0
    empty_menus = 0
    reasons: collections.Counter[str] = collections.Counter()
    for transcript in transcripts:
        sentences = split_sentences(transcript)
        menu = selectable_sentences(sentences)
        if not menu:
            empty_menus += 1
            continue
        for index in range(1, len(menu) + 1):
            selection = materialize_selection([index], menu, len(menu))
            if not selection:
                continue
            total += 1
            citation = ground_claim(selection[0], sentences)
            if citation.grounded:
                grounded += 1
            else:
                # Reason strings are fixed verifier vocabulary, never transcript text.
                reasons[citation.reason] += 1
    return total, grounded, reasons, empty_menus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON list with 'transcript' keys")
    args = parser.parse_args(argv)

    raw = json.loads(args.input.read_text(encoding="utf-8"))
    transcripts = [
        str(item["transcript"])
        for item in raw
        if isinstance(item, dict) and str(item.get("transcript", "")).strip()
    ]
    if not transcripts:
        print("no transcripts in input", file=sys.stderr)  # noqa: T201
        return 2

    total, grounded, reasons, empty_menus = measure(transcripts)
    pct = (100.0 * grounded / total) if total else 0.0
    print(f"transcripts:        {len(transcripts)}")  # noqa: T201
    print(f"  no selectable sentence: {empty_menus} (legacy free-text path)")  # noqa: T201
    print(f"selectable sentences: {total}")  # noqa: T201
    print(f"grounded when selected: {grounded}/{total} ({pct:.1f}%)")  # noqa: T201
    if reasons:
        print("\nGUARANTEE VIOLATIONS (a verbatim sentence that did not ground):")  # noqa: T201
        for reason, count in reasons.most_common():
            print(f"  {count:4d}  {reason}")  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
