# PR-wer-01 Line #34 Execution Report

Issue: `#34 [PR-wer-01] Privacy-safe pilot meeting recording protokolu`

## Purpose

Define a safe protocol for collecting a 5-10 minute Workcube-internal pilot
meeting recording that can later be used in WER/model evaluation.

## Plan Requirement

The issue asks for:

- Workcube internal pilot meeting recording, 5-10 minutes
- explicit consent flow with UI modal and email notification
- KVKK ADR-0030 placeholder alignment
- privacy-safe Vault-encrypted storage
- manual ground-truth transcript, about 2 hours of effort
- demographic balance, Turkish accents, optional Kurdish transitions

## What Changed

- Added protocol:
  - `docs/wer-pilot-meeting-recording-protocol.md`
- Added UI consent modal copy:
  - `docs/templates/wer-pilot-consent-modal.md`
- Added consent email template:
  - `docs/templates/wer-pilot-consent-email.md`
- Added encrypted storage manifest template:
  - `docs/templates/wer-pilot-storage-manifest.template.json`
- Added manual ground-truth transcript template:
  - `docs/templates/wer-pilot-ground-truth.template.json`
- Added this execution report:
  - `docs/pr-wer-01-line-34-execution-report.md`

## Completion Status

Documentation/protocol part is complete.

Real pilot recording is not created by Codex because this issue is tagged
`operator-action` and requires real participant consent. Creating a fake or
unconsented recording would violate the purpose of the issue.

## No-Sapma Assessment

| Requirement | Result | Status |
|---|---|---|
| 5-10 min Workcube internal pilot meeting | Protocol defines target and operator checklist | Ready for operator action |
| Explicit consent flow | UI modal + consent metadata defined | Complete |
| Email notification | Email template added | Complete |
| ADR-0030 placeholder alignment | KVKK boundaries and no-log/no-git/no-customer-data rules written | Complete |
| Privacy-safe storage | Vault-encrypted storage manifest template added | Complete |
| Manual transcript ground truth | JSON template and 2h workflow added | Complete |
| Demographic balance | Turkish accent/voice/speed/code-switch guidance added | Complete |

## Controlled Boundary

The only part not executed is the real human recording. This is not a technical
implementation gap; it is the explicit operator/human-consent boundary of the
issue. The protocol tells the operator exactly what must be done before #35 can
use the pilot meeting as a WER dataset.

## Validation

Docs were checked for the required #34 keywords and paths:

```bash
rg -n "consent|Vault|ground truth|Kurdish|KVKK|operator-action|5-10" docs
```

Expected files exist:

```text
docs/wer-pilot-meeting-recording-protocol.md
docs/templates/wer-pilot-consent-modal.md
docs/templates/wer-pilot-consent-email.md
docs/templates/wer-pilot-storage-manifest.template.json
docs/templates/wer-pilot-ground-truth.template.json
docs/pr-wer-01-line-34-execution-report.md
```

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Recording without full consent | KVKK/compliance risk | Protocol blocks recording unless all consented |
| Customer/PII content enters pilot audio | WER dataset unusable | Privacy-safe content rules and rejection path |
| Raw audio committed to git | Data exposure | Protocol explicitly forbids raw audio in git |
| Storage not actually Vault-encrypted | Pilot cannot be accepted | Storage manifest requires encrypted URI + Vault path |
| Manual transcript inconsistent | WER score unreliable | second-review spot check required |

## Next Gate For #35

#35 should not consume pilot data until the operator provides:

- recording id
- consent manifest
- encrypted storage manifest
- completed manual ground truth
- PII/customer-data screening result

## Next Operator Actions To Actually Record The Pilot

The protocol is ready, but the real pilot meeting still needs operator/human
execution. The next operational actions are:

1. Halil Bey or the assigned operator approves this protocol for pilot use.
2. Operator selects 2-5 internal Workcube participants.
3. Operator confirms no customer/personal/secret data will be discussed.
4. Operator chooses the 5-10 minute pilot meeting date/time.
5. Operator defines the consent owner and withdrawal contact.
6. Operator confirms the encrypted storage target and Vault path.
7. UI consent modal copy is shown to every participant.
8. Consent email is sent after consent.
9. Pilot audio is recorded only if every participant consents.
10. Raw audio is stored only in encrypted storage; never in git.
11. Manual ground-truth transcript is prepared with the JSON template.
12. A second reviewer spot-checks at least 20% of transcript segments.
13. Only then can #35 use the pilot as one WER dataset.

Suggested message to Halil Bey/operator:

```text
#34 privacy-safe pilot meeting protokolu hazir.
Gercek kayit icin operator onayi, katilimci listesi, riza sahibi,
encrypted storage/Vault path ve kayit tarihi gerekiyor.

Kimler katilacak?
Kayit tarihi ne olacak?
Vault/encrypted storage hedefi ne olacak?
Riza/onay sahibi kim olacak?
```

AG-019 staging resource gate pending; implementation validated locally only.
