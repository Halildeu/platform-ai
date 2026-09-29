# Scheduled post-processing diarization — integration design (#3746)

Tracked by Halildeu/platform-k8s-gitops#3746, Product Slice #3399.
Status: DESIGN (implementation slices listed at the end; nothing here enables
runtime, changes provider defaults, or claims customer acceptance).
Anchors: ADR-0033 (post-processing placement, pyannote 3.1 primary, GPU
scheduling boundary), ADR-0036 (no raw-audio persistence),
`docs/faz-24-internal-speaker-adapter-3746.md` (PR #348 adapter preparation).

## 1. Measured current state (origin/main evidence)

Producer side (platform-ai @ 732da87):

- The live production path is the audio-gateway **HTTP window forward** to
  live-stt `/transcribe` (the WS bridge is default-off, backend #184). Windows
  arrive as in-RAM uploads; live-stt answers text-only segments and keeps no
  session state. The WS `final` event schema pins `additionalProperties:false`
  (`docs/contracts/ws-stream-events.schema.json`), and internal STT emits no
  word timings anywhere under `services/`.
- live-stt holds only the **uncommitted** tail of audio (≈37 s bound,
  `stream.py` L763-770); committed audio is discarded on each commit
  (`advance_segment`). Nothing in platform-ai retains whole-session PCM.
- final-stt-service is not deployed, consumes 10-15 s WAV **file paths** under
  `FINAL_STT_AUDIO_ROOT`, and conflicts with ADR-0036 as a diarization host.
- diarization-service is a synchronous `POST /diarize` with an `UploadFile`
  layer that can spool >~1 MiB bodies to disk, a 504 timeout that does **not**
  terminate inference, no queue, no revision pin, and a Dockerfile port (8300)
  that collides with meeting-ai on the GPU host. After PR #348 the adapter
  itself is RAM-only (`BytesIO`, `max_speakers` honored, single-flight lock).
- The GPU host runs exactly two managed tasks (`platform-ai-live-stt` :8200,
  `platform-ai-meeting-ai` :8300); `task-action-contract.ps1`, `install.ps1`,
  `update.ps1`, `migrate-task-actions.ps1` and ~45 test references hard-code
  the pair. There is no `DIA_*` env allowlist.
- Reusable in-repo patterns: live-stt's supervised spawn-process inference
  (terminate/kill/respawn, hard timeout — the killable-inference precedent)
  and meeting-ai's `ReadyEventConsumerRuntime` (PEL ownership, encrypted
  metadata-only inbox, XAUTOCLAIM, DLQ, ack-after-durable-state).

Consumer side (platform-backend origin/main):

- `SpeakerAttribution` v2 (`audioGateway.directSttTranscriptResult.v2`) is the
  settled contract: session-scoped deterministic `scope` UUID (includes the
  transport `epoch`), `Turn{speaker, textStart, textEnd (UTF-16), startMs,
  endMs}`, labels `S[1-9]\d{0,2}|SPEAKER_\d{2,3}|UU` (pyannote-style accepted),
  ≤512 turns, non-overlapping text spans, overlapping acoustic spans allowed,
  `UU` → `speakerId()=null`.
- transcript-service persists attribution **inside the window ingest event**
  (`DirectSttTranscriptIngestionService.upsert`); the idempotency comparison
  includes attribution, so re-publishing a committed window with different
  attribution is classified as replay conflict and dropped. Replay is not a
  delivery channel — by design and per the #3746 boundary.
- The finalize canonical snapshot (`FinalizedTranscriptSnapshotCodec`,
  `StoredSegment(text,start,end)`) carries no speaker and is hash-sealed.
- `/api/v1/admin/transcripts` returns **segment rows**; the web meeting view
  reads `speakerId` per segment (`meeting-api.ts:459`, default "Konuşmacı").
  Turn-level attribution is not yet consumed by the web transcript list.
- Redis Streams consumer scaffolding exists (`DirectSttTranscriptResultStreamConsumer`:
  consumer group, ack-after-upsert, metadata-only DLQ, default-off property).

## 2. Decisions

### D1 — Placement: post-session batch inside the live-stt host process family
Diarization runs **after** a session ends, never in live final/EOF handling,
and never extends the EOF budget (ADR-0033). It executes on the GPU host where
the audio already flows; no third managed task and no new port: the batch
worker is owned by the live-stt process (background, single-flight), reusing
the supervised spawn pattern so inference is killable. The diarization-service
**HTTP surface is not used** (spool + non-terminating timeout); the PR #348
`PyannoteDiarizer` adapter is consumed as a library from the spawned worker.
This dissolves the :8300 collision and the 5-file task-contract expansion.

### D2 — Bounded transient session audio store (new, live-stt)
A per-session, RAM-only PCM store accumulates the float32/PCM16 windows that
already reach live-stt, keyed by an opaque session key supplied by the caller
(D4). Properties:

- Hard cap (default 120 min @16 kHz mono ≈ 230 MB; env-tunable). On overflow
  the store **cancels** that session's diarization and drops the audio —
  fail-closed to "no attribution" rather than partial/wrong labels.
- TTL/idle janitor: sessions with no frames and no finish signal for
  `store_idle_ttl` (default 15 min) are dropped.
- Lifecycle ends at: successful hand-off to the worker, cancel, TTL, or
  process shutdown. Audio never touches disk, logs, or metrics (ADR-0036);
  the store's observability is byte/section counts only.
- Default-off (`STT_SESSION_AUDIO_STORE_ENABLED=false`) behind the live-stt
  env allowlist (typed schema variant), so rollout is a config decision.

### D3 — Alignment: segment-granularity, session-stable labels
Internal STT has no word timings, so turns are aligned at window/segment
granularity: pyannote's time-ranged anonymous turns are intersected with the
persisted segment `[start,end)` ranges; UTF-16 `textStart/textEnd` derive from
window text boundaries (no intra-word splits). A window with multiple speakers
keeps scalar `speaker_id=null` and a multi-entry `turns` list — exactly the
existing contract semantics. The whole session is clustered in **one** batch,
so labels are structurally session-stable; per-window independent clustering
is not performed and is not claimed as continuity. Overlap regions keep
overlapping acoustic spans; low-confidence/unmatched regions emit `UU`.

### D4 — Identity and finish signal come from the gateway (HTTP contract)
live-stt's WS/HTTP surfaces carry no tenant/meeting/session identity today,
and the WS `final` schema is closed. Identity therefore rides the **HTTP
request path** the gateway already owns:

- The gateway adds an opaque session key header (e.g. `X-Stt-Session-Key`:
  HMAC/opaque, no tenant PII in the value) to each `/transcribe` window POST.
  `TranscriptResult` parsing is `ignoreUnknown`, and the request header is
  additive — no breaking contract change.
- On session finish (the existing gateway finish/close path), the gateway
  calls a new small live-stt endpoint `POST /session/{key}/finish` carrying
  the identity envelope (tenant, meeting, sourceSessionId, epoch, expected
  total samples). live-stt validates expected-vs-received sample counts
  (mismatch → cancel, no attribution), then schedules the batch job.
- If the finish call never arrives, the TTL janitor drops the audio.

### D5 — Delivery: new session-level attribution event + pre-finalize apply
The worker publishes `directSttSessionAttribution.v1` to the existing Redis
plane (metadata only: identity envelope + `SpeakerAttribution` v2 payload —
spans and times, no audio, no text). transcript-service gains a consumer
(clone of the direct-STT consumer pattern, default-off) that **applies**
attribution to the session's DRAFT segment rows (`speaker_attribution` +
scalar `speaker_id` only for single-speaker windows), through the existing
erasure/retention fences, with its own audit row. This is a distinct service
flow, not an ingest replay; committed windows are never re-published.

Ordering: the finalize min-wait (~6 min) comfortably covers the expected
GPU batch time (RTF ≈ 0.025 → 60 min audio ≈ 90 s). If attribution misses the
finalize window, finalize proceeds without it (content is never delayed);
late attribution still lands on segment rows (the canonical snapshot carries
no speaker, so no seal is violated), and the admin/segment read path — which
the web actually renders — picks it up. An attribution-aware re-finalize is
explicitly out of scope (separate decision).

### D6 — GPU capacity gate before every batch
Before loading pyannote (~2.2 GB VRAM), the worker checks free VRAM and
defers (bounded retries with backoff) while live decode or an Ollama analysis
holds the budget — ADR-0033's explicit co-load boundary. The model is loaded
per batch and released after (no third resident model). Note: ADR-0033 says
"8 GB VRAM" while `issue-40-hardware-decision-final.md` says 12 GB; the
capacity gate reads actual free VRAM at runtime, so the doc conflict does not
affect behavior (flagged for a doc fix in the D1 slice).

### D7 — Measurement and acceptance (unchanged bar)
Same synthetic Turkish multi-speaker/noise/overlap corpus, exact GPU
artifact: corpus DER (collar 0.25 and zero-collar, overlap included) under the
accepted 30% ceiling; RTF + peak VRAM delta + co-load interference evidence;
WER unchanged (text path untouched); session-stable label check (same voice in
distant windows keeps one label); durable persistence + browser reopen on
TEST; negative paths (cap overflow → clean cancel + no attribution; capacity
deferral measured). Speechmatics-based #3753 acceptance is preserved, not
relabeled.

## 3. Privacy and security boundaries

- ADR-0036 intact: session PCM exists only in bounded RAM inside processes it
  already flows through; no disk, no object store, no new external transfer
  (the GPU host is already the audio processing locus).
- Anonymous labels only (`S*/SPEAKER_*/UU`); no embeddings retained, no
  voiceprints, no identity mapping (ADR-0035 legal gate untouched).
- The attribution event carries spans/times only — no audio, no transcript
  text. Redis stays inside the existing secured plane (stage/prod `rediss://`).
- Provider default stays `mock`; every new env key is allowlisted and
  default-off. No silent provider switch.

## 4. Implementation slices (PR order)

1. **AI-D1 (platform-ai, doc-only)** — this design + ADR-0033 amendment note
   (scheduled-batch integration + VRAM figure fix + stale "stub (501)" README
   line correction).
2. **AI-D2 (platform-ai)** — live-stt bounded session audio store: opt-in env
   keys in the typed allowlist (`STT_SESSION_AUDIO_STORE_ENABLED`,
   `STT_SESSION_AUDIO_CAP_BYTES`, `STT_SESSION_AUDIO_IDLE_TTL_SEC`), store +
   janitor + unit tests. No diarization yet, no behavior change when off.
3. **AI-D3 (platform-ai)** — batch worker: supervised spawn around the PR #348
   adapter (library use, pinned model revision), VRAM capacity gate,
   `POST /session/{key}/finish` endpoint, Redis producer for
   `directSttSessionAttribution.v1`, `DIA_*`/`STT_DIAR_*` allowlist keys.
4. **BE-D4 (platform-backend)** — gateway session-key header + finish call;
   transcript-service attribution consumer (default-off) + apply flow + audit
   + tests.
5. **AI/GITOPS-D5** — GPU corpus measurement (DER/RTF/VRAM/co-load), TEST
   rollout of the default-off flags, NEW persisted TEST/browser journey,
   acceptance evidence on #3746.

Each slice is independently revertible; every runtime flag ships default-off.
