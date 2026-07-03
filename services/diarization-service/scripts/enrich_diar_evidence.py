"""One-off enrichment: turn raw diar_matrix.py rows into decision-gate-ready
evidence (#235 revision-pin follow-up).

diar_matrix.py is a pure measurement tool and does not know the gate's
metadata contract (dataset_kind, deployment_mode, license_status, biometric
boundary flags, evidence_hash). This script adds exactly the same fields the
existing docs/evidence/diar-decision-pilot-2026-07-02.jsonl row already
carries, so the pilot and overlap rows are consistent with each other and
with what diar_decision_gate.py requires — plus an `evidence_hash` on the
overlap rows too (Halil's non-blocking review note: pilot has one, overlap
didn't).

Usage:
    python scripts/enrich_diar_evidence.py \
        --in C:\\faz24-pilot\\diar-decision-pilot-revised.jsonl --pilot \
        --out docs/evidence/diar-decision-pilot-2026-07-03.jsonl
    python scripts/enrich_diar_evidence.py \
        --in C:\\faz24-pilot\\diar-overlap-revised.jsonl --pilot \
        --out docs/evidence/diar-overlap-results-2026-07-03.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# Same metadata every row in the accepted #161 decision evidence carries
# (docs/evidence/diar-decision-pilot-2026-07-02.jsonl) — kept identical so
# this revision-pin refresh doesn't silently change the decision posture.
_FIXED_METADATA = {
    "dataset_kind": "pilot-meeting",
    "deployment_mode": "self-hosted",
    "license_status": "approved",
    "biometric_processing": False,
    "speaker_identity_mapping": False,
    "voiceprint_enabled": False,
}


def _evidence_hash(row: dict[str, object]) -> str:
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enrich_row(row: dict[str, object]) -> dict[str, object]:
    enriched = dict(row)
    enriched.pop("fixture_kind", None)  # replaced by dataset_kind below
    enriched.update(_FIXED_METADATA)
    # Hash the row BEFORE adding evidence_hash itself (can't hash a field
    # that includes its own value).
    enriched["evidence_hash"] = _evidence_hash(enriched)
    return enriched


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    lines = [
        line
        for line in args.in_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [enrich_row(json.loads(line)) for line in lines]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} enriched row(s) -> {args.out}")


if __name__ == "__main__":
    main()
