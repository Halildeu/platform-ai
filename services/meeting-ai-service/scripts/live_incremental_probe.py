"""Opt-in real-model probe with synthetic speech; emits metadata, never meeting text.

Run from the service directory with PYTHONPATH=. and the qualified runtime's
MAI_* environment. This measures model updates, not microphone-to-phone latency.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

from app.core.config import Settings
from app.services.analyze import MeetingAnalysisService
from app.services.citation import split_sentences


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-update-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if not 0 < args.max_update_seconds <= 120:
        parser.error("max-update-seconds must be between 0 and 120")
    settings = Settings()
    if settings.backend != "ollama" or not settings.ollama_expected_digest:
        parser.error("Requires the qualified Ollama backend and expected model digest")
    service = MeetingAnalysisService(settings)
    # Explicit full names keep owner verification in the same source sentence.
    stages = [
        ("Ayşe sunum dosyasını cuma günü hazırlayacak.", [], [0]),
        (
            "Sunumu çevrim içi yapmaya karar verdik. Mehmet bütçe tablosunu kontrol edecek.",
            [1],
            [0, 2],
        ),
        ("Ayşe için sunum dosyası hazırlama görevini iptal ettik.", [1], [2]),
        (
            "Mehmet'in bütçe kontrolü görevi iptal edildi. Zeynep bütçe tablosunu kontrol edecek.",
            [1],
            [5],
        ),
        ("Katılımcılar gündemin diğer konularını görüştü.", [1], [5]),
    ]
    transcript = ""
    cursor = None
    rows: list[dict[str, object]] = []
    for version, (addition, decisions, actions) in enumerate(stages, 1):
        transcript = (transcript + " " + addition).strip()
        started = time.monotonic()
        try:
            result = service.analyze(transcript, live=True, live_cursor=cursor)
        except Exception as exc:  # noqa: BLE001 — metadata only, fail the probe
            rows.append({"version": version, "status": "error", "error_class": type(exc).__name__})
            break
        elapsed = time.monotonic() - started
        source = split_sentences(transcript)
        expected_decisions = {source[i].text for i in decisions}
        expected_actions = {source[i].text for i in actions}
        quality = (
            set(result.decisions) == expected_decisions
            and {item.text for item in result.action_items} == expected_actions
            and result.ungrounded_count == 0
        )
        rows.append(
            {
                "version": version,
                "elapsed_seconds": round(elapsed, 3),
                "within_update_budget": elapsed <= args.max_update_seconds,
                "quality_pass": quality,
                "decision_count": len(result.decisions),
                "action_count": len(result.action_items),
                "source_chars": len(transcript),
                "cursor_returned": result.live_cursor is not None,
            }
        )
        cursor = result.live_cursor
    passed = len(rows) == len(stages) and all(
        row.get("quality_pass") and row.get("within_update_budget") for row in rows
    )
    sys.stdout.write(
        json.dumps(
            {
                "schema": "live-incremental-model-probe-v1",
                "synthetic": True,
                "status": "pass" if passed else "fail",
                "utc": datetime.now(UTC).isoformat(),
                "model": settings.ollama_model,
                "expected_digest": settings.ollama_expected_digest,
                "think": settings.ollama_think,
                "max_update_seconds": args.max_update_seconds,
                "phone_acceptance": False,
                "rows": rows,
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
