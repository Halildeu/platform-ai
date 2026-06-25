# ADR-0036: Recording / Archive Plane Boundary

- Status: **ACCEPTED — LIVE PATH RED, FUTURE OPT-IN ONLY** (2026-06-25)
- Issue: `#185 [Faz24] C: Recording/archive plane (MinIO) — backlog ADR`
- Scope: whether Faz 24 stores raw meeting audio in MinIO/S3 as part of the
  normal product path.

## Context

Faz 24's current runtime path is designed around data minimization:

1. `audio-gateway-service` receives chunks from the recorder.
2. Redis Streams carry hash and metadata only.
3. direct-STT is gated behind app-mTLS, `CHUNK_FORWARDED_TO_COMPUTE_PLANE`
   audit, and no-raw-audio-persistence proof.
4. transcripts and intelligence outputs have separate retention gates.

Enterprise meeting products often expose a recording library, replay, and
post-meeting reprocessing surface. That is useful for adoption, but for this
product it materially expands the KVKK risk surface: raw voice is sensitive,
can become biometric-adjacent when combined with diarization/voiceprint, and
requires stronger retention, erasure, access-control, legal-hold, and breach
response evidence than metadata-only STT transit.

ADR-0035 selects recorder as the primary capture path and gates voiceprint
live-processing on KVKK m.6 legal/consent work. This ADR keeps that capture path
usable without silently adding a recording library or raw-audio retention.

#185 was opened to prevent the team from accidentally treating MinIO as the
default raw-audio store simply because a bucket exists. The bucket and lifecycle
support are infrastructure primitives; they are not product acceptance for a
recording archive.

## Decision

The default Faz 24 live path **does not persist raw meeting audio**. Raw audio
must stay transient unless a later explicit recording/archive feature is opened
and accepted through the gates below.

The recording/archive plane is therefore:

| Surface | Decision |
|---|---|
| Live direct-STT / recorder smoke | **No raw-audio persistence**; prove no object write as part of #182 evidence. |
| Redis Streams | Hash + metadata only; no audio bytes, no transcript text. |
| MinIO `meeting-audio` bucket | Infrastructure available with 7-day lifecycle, but not a default product path. |
| Replay / reprocess / legal-hold archive | Future opt-in feature only; requires a new PR/issue with the gate evidence below. |
| Customer/pilot audio in issue comments, logs, PRs, or evidence files | Forbidden. Evidence must be metadata-only and redacted. |

This is a product boundary, not only an implementation detail. A future code
change that stores raw audio must be reviewed as a privacy-sensitive feature,
not as a harmless persistence helper.

## Required Gates Before Any Archive Activation

Any future recording/archive activation must provide all of these before raw
audio is stored:

| Gate | Required evidence |
|---|---|
| Purpose and legal basis | Explicit product need such as user-visible recording library, approved reprocess path, or legal hold; linked legal/owner decision. |
| Consent and notice | UI/flow proof that participants are informed and consent state is recorded before recording. |
| Tenant isolation | Object keys begin with `s3://meeting-audio/{tenant_id}/...`; no user names, emails, phone numbers, room names, or free-text titles in keys. |
| Encryption and key control | SSE/KMS or equivalent approved key-management evidence; service-account-only access. |
| Retention | `meeting-audio` lifecycle remains at 7 days maximum unless this ADR is reopened by a new legal/owner decision; retention must be covered by `scripts/retention_gate.py`. |
| Early deletion | Transcript-complete or user-requested erasure path with metadata-only destruction audit; audit failure must block deletion claims. |
| Legal hold | Hold state, owner approval, expiry/review cadence, and audit record; legal hold must not become silent indefinite retention. |
| Access audit | Metadata-only audit for read/write/delete/hold events; no transcript/audio/object secret in audit payload. |
| Evidence privacy | No raw audio path, presigned URL, transcript, prompt/response, or participant identifier in CI logs, issue comments, or PR bodies. |
| Rollback | Disable-write path plus object inventory/delete verification for test activation. |

Until those gates pass, the only acceptable `meeting-audio` evidence is
bucket/lifecycle readiness and negative proof that the live STT path did not
write raw audio.

## Consequences

- #182 direct audio e2e must include no-raw-audio-persistence evidence. A
  transcript result alone is not enough if raw audio may have been stored.
- #156 retention readiness remains the go-live guard for storage behavior.
  MinIO lifecycle evidence is necessary but not sufficient; DB cleanup and
  VERBIS/operator evidence still matter.
- #160 capture work can continue with recorder/client UX, but a recording
  library is not part of the initial capture acceptance unless this ADR is
  reopened.
- #161 and #162 quality/intelligence gates may use approved pilot metadata and
  redacted transcripts according to their runbooks; this ADR does not authorize
  storing the underlying raw audio.

## Reopen Triggers

Reopen this ADR only if at least one of these becomes true:

- a customer/pilot requires replayable recordings as a product feature;
- legal hold or dispute workflow requires raw audio retention;
- post-meeting reprocessing cannot be satisfied from transcript/segments alone;
- a migration/import flow needs bounded raw-audio preservation.

Reopening requires a new issue/PR and the required gates above. It must not be
bundled into an unrelated direct-STT, WER, diarization, or meeting-ai change.

## Current Status for #185

The ADR boundary is now explicit: recording/archive is not on the live path and
must stay backlog/opt-in until a future owner/legal/product decision opens it.
No runtime, MinIO, Kubernetes, or production state is changed by this ADR.
