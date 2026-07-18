ALTER TABLE meeting_transcript_ready_inbox
    ADD COLUMN dead_reason TEXT
    CHECK (dead_reason IN ('RETRY_EXHAUSTED', 'TERMINAL', 'CONFLICT', 'POISON'));

ALTER TABLE meeting_transcript_ready_inbox
    ADD COLUMN redrive_count INTEGER NOT NULL DEFAULT 0
    CHECK (redrive_count >= 0);

ALTER TABLE meeting_transcript_ready_inbox
    ADD COLUMN last_redriven_at REAL;

ALTER TABLE meeting_transcript_ready_inbox
    ADD COLUMN last_redrive_reference TEXT;
