#!/usr/bin/env python3
"""Requeue one analysis-result DLQ row without exposing its encrypted payload."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.services.durable_outbox import PayloadCipher, SqliteOutboxStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--analysis-run-id", type=uuid.UUID)
    action.add_argument("--list-dead", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.ingestion_enabled:
        parser.error("MAI_INGESTION_ENABLED must be true")
    store = SqliteOutboxStore(
        settings.ingestion_store_path,
        PayloadCipher(
            settings.ingestion_payload_encryption_keys(),
            settings.ingestion_active_key_id,
            lookup_key=settings.ingestion_lookup_key(),
        ),
        max_rows=settings.ingestion_max_rows,
    )
    if args.list_dead:
        for item in store.list_dead(limit=args.limit):
            created_at = datetime.fromtimestamp(item.created_at, UTC).isoformat()
            updated_at = datetime.fromtimestamp(item.updated_at, UTC).isoformat()
            print(  # noqa: T201
                f"analysis_run_id={item.analysis_run_id} attempts={item.attempt_count} "
                f"created_at={created_at} updated_at={updated_at} "
                f"error_code={item.last_error_code or '-'}"
            )
        return 0

    run_id = str(args.analysis_run_id)
    if not store.requeue_dead(run_id):
        print(f"not-requeued analysis_run_id={run_id}", file=sys.stderr)  # noqa: T201
        return 2
    print(f"requeued analysis_run_id={run_id}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
