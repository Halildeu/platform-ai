CREATE TABLE meeting_transcript_ready_inbox (
    event_key TEXT PRIMARY KEY,
    payload_sha256 TEXT NOT NULL
        CHECK (length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('RECEIVED', 'PROCESSING', 'OUTBOXED', 'DEAD')),
    tenant_id TEXT,
    meeting_id TEXT,
    session_id TEXT,
    finalization_version INTEGER,
    analysis_run_id TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    lease_recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (lease_recovery_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_error_code TEXT,
    dlq_published_at REAL,
    CHECK (
        (state = 'PROCESSING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (state != 'PROCESSING' AND lease_owner IS NULL AND lease_until IS NULL)
    ),
    CHECK (
        state = 'DEAD'
        OR (
            tenant_id IS NOT NULL
            AND meeting_id IS NOT NULL
            AND session_id IS NOT NULL
            AND finalization_version IS NOT NULL
            AND finalization_version >= 1
            AND analysis_run_id IS NOT NULL
        )
    )
);

CREATE INDEX idx_meeting_transcript_ready_due
    ON meeting_transcript_ready_inbox(state, next_attempt_at, lease_until, created_at);

CREATE INDEX idx_meeting_transcript_ready_analysis_run
    ON meeting_transcript_ready_inbox(analysis_run_id);
