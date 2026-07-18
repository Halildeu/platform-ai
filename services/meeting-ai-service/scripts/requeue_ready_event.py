#!/usr/bin/env python3
"""List or rearm one retry-exhausted ready event without exposing event content."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.services.durable_outbox import PayloadCipher, SqliteOutboxStore
from app.services.ready_event_inbox import SqliteReadyEventInbox


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--lookup-key")
    action.add_argument("--list-dead", action="store_true")
    parser.add_argument("--audit-reference")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.ready_consumer_enabled:
        parser.error("MAI_READY_CONSUMER_ENABLED must be true")
    store = SqliteOutboxStore(
        settings.ingestion_store_path,
        PayloadCipher(
            settings.ingestion_encryption_keys(),
            settings.ingestion_active_key_id,
        ),
        max_rows=settings.ingestion_max_rows,
    )
    inbox = SqliteReadyEventInbox(
        store,
        max_rows=settings.ready_consumer_inbox_max_rows,
        max_failures=settings.ready_consumer_max_failures,
    )

    if args.list_dead:
        for item in inbox.list_dead(limit=args.limit):
            updated_at = datetime.fromtimestamp(item.updated_at, UTC).isoformat()
            print(  # noqa: T201
                f"lookup_key={item.lookup_key} event_key={item.event_key} "
                f"failures={item.failure_count} "
                f"reason={item.dead_reason.value if item.dead_reason else '-'} "
                f"updated_at={updated_at} redrives={item.redrive_count} "
                f"error_code={item.last_error_code or '-'}"
            )
        return 0

    if not args.audit_reference:
        parser.error("--audit-reference is required with --lookup-key")
    lookup_key = str(args.lookup_key)
    if not inbox.rearm_retry_exhausted(
        lookup_key,
        audit_reference=str(args.audit_reference),
    ):
        print(f"not-rearmed lookup_key={lookup_key}", file=sys.stderr)  # noqa: T201
        return 2
    print(  # noqa: T201
        f"rearmed lookup_key={lookup_key}; exact original producer event replay is now required"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
