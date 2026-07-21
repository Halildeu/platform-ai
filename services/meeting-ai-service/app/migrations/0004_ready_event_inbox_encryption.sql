CREATE TABLE meeting_transcript_ready_inbox_v4 (
    event_key_digest TEXT PRIMARY KEY
        CHECK (length(event_key_digest) = 64 AND event_key_digest NOT GLOB '*[^0-9a-f]*'),
    identity_key_id TEXT NOT NULL,
    identity_nonce BLOB NOT NULL CHECK (length(identity_nonce) = 12),
    identity_ciphertext BLOB NOT NULL CHECK (length(identity_ciphertext) >= 16),
    state TEXT NOT NULL CHECK (state IN ('RECEIVED', 'PROCESSING', 'OUTBOXED', 'DEAD')),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    lease_recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (lease_recovery_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_error_code TEXT,
    dlq_published_at REAL,
    dead_reason TEXT CHECK (
        dead_reason IS NULL
        OR dead_reason IN ('RETRY_EXHAUSTED', 'TERMINAL', 'CONFLICT', 'POISON')
    ),
    redrive_count INTEGER NOT NULL DEFAULT 0 CHECK (redrive_count >= 0),
    last_redriven_at REAL,
    last_redrive_reference TEXT,
    CHECK (
        (state = 'PROCESSING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (state != 'PROCESSING' AND lease_owner IS NULL AND lease_until IS NULL)
    )
);
