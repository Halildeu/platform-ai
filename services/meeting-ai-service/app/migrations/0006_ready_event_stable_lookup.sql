-- Migration 6 is executed by SqliteOutboxStore because every encrypted
-- identity must be authenticated and rebound in one transaction.
SELECT 1;
