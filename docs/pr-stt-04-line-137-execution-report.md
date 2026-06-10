# PR-stt-04 Line #137 Execution Report

Issue: `#137 [PR-stt-04] live-stt-service Redis Streams consumer - audio:chunks:p00..p31, group live-stt-v1 (ADR-0031 D3)`

Canonical source: live GitHub issue body (Halildeu/platform-ai #137) — opened as
the P2-2 decision of the platform-backend PR #534 Cross-AI review (PR-stt-03
issues #20-22 closed with subprocess-worker scope, not retrofitted).

## Objective

Consume the gateway dispatcher's partitioned Redis Streams
(`audio:chunks:p00..p31`, ADR-0031 D3) with consumer group `live-stt-v1` and
feed validated chunk envelopes to the STT pipeline behind a pluggable handler
seam.

## Producer contract (fixed by platform-backend PR #534)

| Contract item | Consumer implementation |
|---|---|
| 32 partitions `audio:chunks:p<NN>` (`%02d`) | `partition_keys()` generates `p00..p31`; single `XREADGROUP` covers all |
| Consumer group `live-stt-v1` | `ensure_groups()` XGROUP CREATE mkstream per partition, BUSYGROUP tolerated |
| Dedup on `messageId` (`sessionId:chunkSeq`), NOT entry id | `_LruSeenSet` bounded LRU keyed by messageId; duplicate → XACK without re-handling |
| Hash-only payload (ADR-0030 PII boundary) | `ChunkEnvelope` carries sha256 + routing metadata; logs print hash prefix only, never payload |
| XACK + bounded trim consumer responsibility | success/duplicate/poison → XACK; `XTRIM maxlen~` after each touched batch |
| Producer watches XPENDING (lag → 429) | only unexpected handler failures stay pending; poison messages acked so they never wedge a partition |
| Crash/restart recovery | `claim_stale()` XAUTOCLAIM (min idle `STT_CHUNK_CLAIM_IDLE_MS`) every N loops, claimed entries reprocessed through the same dedup path |

## Files

| File | Change |
|---|---|
| `app/services/chunk_consumer.py` | New: `AudioChunkConsumer`, `ChunkEnvelope`, `ChunkHandler` seam, `LoggingChunkHandler`, LRU dedup, metrics |
| `app/core/config.py` | New `STT_CHUNK_*` / `STT_REDIS_URL` settings (default OFF — CI/CPU unaffected) |
| `app/main.py` | Lifespan: opt-in daemon-thread start/stop of the consumer |
| `requirements.txt` | `redis==5.1.1` (matches final-stt-service pin) |
| `tests/unit/test_chunk_consumer.py` | 13 unit tests (FakeRedis pattern from final-stt-service) |

## Design Notes

- **Handler seam:** the stream payload is hash-only — resolving audio bytes and
  scheduling transcription is the next integration slice. `ChunkHandler`
  protocol isolates that; default `LoggingChunkHandler` proves the transport
  with PII-safe logs.
- **Ack discipline vs producer XPENDING gate:** the producer rejects chunks
  with 429 when pending grows or the oldest entry idles too long, so the
  consumer ACKs everything it has dealt with (including poison + duplicates)
  and leaves ONLY genuinely retryable failures pending.
- **Dedup scope:** partition assignment is deterministic per session
  (`hash(tenantId+sessionId) % 32` producer-side), so messageId dedup state is
  naturally partition-local; the LRU bound (default 8192) caps memory.
- **Metrics:** `stt_chunk_consumer_up`, `stt_chunks_consumed_total{result=success|duplicate|invalid|retry}`,
  `stt_chunks_claimed_total` — exposed by the existing `/metrics` endpoint.

## Validation Evidence

| Check | Result |
|---|---|
| `pytest tests/unit/test_chunk_consumer.py` | **13 passed** (2026-06-10 17:20 +03) |
| Full unit suite (`pytest tests/unit`) | **123 passed** (110 pre-existing + 13 new, no regressions) |
| `ruff check` on changed files | All checks passed (3 pre-existing RUF002 Turkish-char warnings in `config.py` docstring untouched — present on main) |
| Coverage highlights | partition format, group creation + BUSYGROUP, messageId dedup across different entry ids, poison-ack, failure-stays-pending, retry-after-recovery, XAUTOCLAIM reprocess, bounded trim, run-loop end-to-end, contract defaults |

## Out of Scope (per issue / follow-up)

- Audio byte resolution + transcription scheduling behind `ChunkHandler` (next slice)
- Container e2e (gateway + Redis + live-stt) — needs PR #534 merged + staging-sw Redis (operator, ADR-0031 D1/D2)
- Result publication stream back to gateway/meeting-ai (separate contract)

## Next Step

Cross-AI peer review per repository policy before upstream merge. Branch on the
contributor fork; PR to Halildeu/platform-ai.
