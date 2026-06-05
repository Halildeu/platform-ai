# WER Pilot Meeting Recording Protocol

Issue: `#34 [PR-wer-01] Privacy-safe pilot meeting recording protokolu`

## Purpose

Create a privacy-safe pilot meeting recording protocol for WER evaluation. This
protocol defines how a 5-10 minute Workcube-internal Turkish pilot meeting can
be recorded, stored, manually transcribed, and used for accuracy measurement
without exposing real customer meeting data.

This protocol does not authorize recording by itself. Recording starts only
after operator approval, explicit participant consent, and ADR-0030 alignment.

## Scope

Included:

- 5-10 minute Workcube-internal pilot meeting recording
- explicit consent flow
- UI modal copy
- email notification copy
- Vault-encrypted storage requirements
- manual ground-truth transcript workflow
- demographic/balance guidance
- access, retention, deletion, and audit checklist

Excluded:

- customer meeting recordings
- hidden/background recording
- production go-live approval
- LLM summarization
- biometric/speaker identity inference beyond optional human transcript labels

## Minimum Recording Profile

| Field | Requirement |
|---|---|
| Duration | 5-10 minutes |
| Language | Turkish primary |
| Setting | Workcube-internal pilot only |
| Participants | 2-5 consenting internal participants |
| Content | Non-customer, non-confidential, synthetic business discussion |
| Audio format | WAV or PCM16, mono preferred, 16 kHz or 48 kHz |
| Transcript | Manual ground truth required before WER use |
| Storage | Encrypted at rest; no local desktop long-term storage |

## Consent Flow

1. Meeting organizer opens the pilot recording flow.
2. UI shows the explicit consent modal from
   `docs/templates/wer-pilot-consent-modal.md`.
3. Every participant must actively consent before recording starts.
4. If any participant declines, recording is not started.
5. Email notification from `docs/templates/wer-pilot-consent-email.md` is sent
   to all participants after consent.
6. Recording state, consent timestamp, participant count, and correlation id are
   logged as metadata only. Raw transcript/audio content must not be logged.

Consent records must include:

- consent version
- participant identifier or internal user id
- meeting id
- recording session id
- timestamp
- purpose: WER/model evaluation
- retention/deletion notice
- withdrawal path

## Privacy-Safe Content Rules

Allowed content:

- synthetic internal planning
- invented project names
- neutral scheduling discussion
- fake action items
- non-sensitive technical terms

Forbidden content:

- customer names
- citizen id / national id
- phone numbers
- personal addresses
- real financial data
- real HR or health information
- secrets, tokens, passwords
- production incident details with identifiable user/customer data

If forbidden content is spoken, the recording is rejected for WER use and must
follow deletion/quarantine handling.

## Storage Requirements

Pilot raw audio must be stored only after encryption is available.

Expected storage pattern:

```text
vault-encrypted object storage
  wer-pilot/
    <recording_id>/
      audio.wav.enc
      ground-truth.json
      consent-manifest.json
      storage-manifest.json
```

Metadata template:

`docs/templates/wer-pilot-storage-manifest.template.json`

Rules:

- no raw audio committed to git
- no transcript text in application logs
- no audio path containing participant name or email
- access limited to WER operators and approved reviewers
- encryption key managed through Vault or approved secret management
- deletion request must remove audio, transcript, and derived WER artifacts

## Manual Ground Truth Workflow

Manual transcript is required before any WER score is accepted.

1. A trained operator listens to the pilot audio.
2. Transcript is written in the template:
   `docs/templates/wer-pilot-ground-truth.template.json`
3. Unclear words are marked with `[inaudible]`.
4. Speaker names are not required. If speaker separation is useful, use neutral
   labels like `SPEAKER_01`, `SPEAKER_02`.
5. A second reviewer spot-checks at least 20% of segments.
6. Only final reviewed ground truth is used by #35 WER matrix.

Expected effort: about 2 hours per 5-10 minute pilot recording, including
review and correction.

## Demographic / Speech Balance

Target balance for the first pilot set:

| Dimension | Target |
|---|---|
| Turkish accents | at least 2 different Turkish speaking styles or accents |
| Gender/voice variety | at least 2 different voice profiles when possible |
| Speaking speed | mix of normal and fast business speech |
| Overlap | short natural interruptions allowed, but avoid chaotic overlap |
| Code-switching | optional short Kurdish phrase/transition if participants explicitly consent |

Kurdish transitions are optional and must not introduce sensitive personal or
political content. They are for ASR robustness only.

## Acceptance Checklist

Recording can be used for WER only if all are true:

- [ ] ADR-0030 alignment accepted or explicitly allowed for pilot
- [ ] all participants explicitly consented
- [ ] consent email sent
- [ ] audio stored in encrypted storage
- [ ] no raw audio committed to git
- [ ] no PII/customer data in the recording
- [ ] storage manifest created
- [ ] consent manifest created
- [ ] manual ground truth completed
- [ ] second reviewer spot-check completed
- [ ] deletion/withdrawal path documented
- [ ] WER report references recording by opaque id only

## Operator Action

This issue has `operator-action`. Codex can prepare protocol and templates, but
cannot create a real Workcube meeting recording or consent on behalf of people.

Before recording, operator must provide:

- pilot date/time
- participants
- consent owner
- approved encrypted storage target
- retention period
- reviewer/operator names

## Link To Next Steps

- #35 uses this pilot output as one dataset in the WER + 8 metric matrix.
- #36 triangulates Common Voice, synthetic/pilot, and pilot-meeting results.
- #37 turns measured evidence into the model decision ADR.
