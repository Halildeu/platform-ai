CREATE TABLE analysis_delivery_outbox (
    analysis_run_id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'IN_FLIGHT', 'DEAD')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_error_code TEXT,
    CHECK (
        (state = 'IN_FLIGHT' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR state != 'IN_FLIGHT'
    )
);

CREATE INDEX idx_analysis_delivery_due
    ON analysis_delivery_outbox(state, next_attempt_at, lease_until, created_at);
