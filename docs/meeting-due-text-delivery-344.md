# Grounded Due Text Delivery

Source enabler: platform-ai#344. Customer journey: Product Slice
Halildeu/platform-k8s-gitops#3399, quality parent #3753.

## Observed Boundary

The first new TEST recording on source `d67ec2d` persisted four expected decisions
and three expected actions, but lost the relative date phrase. Its 112 source rows
equal the frozen observed-ASR fixture exactly. This is not a transcript or gold
normalization change. The persisted result contains null dates and no rejected
claims; the model's intermediate draft was not captured for this recording.

The previous delivery adapter mapped `due_date` only to an ISO `due` timestamp.
`datetime.fromisoformat` cannot parse a relative phrase, so a correctly extracted
and grounded phrase deterministically became null before the encrypted outbox.
The backend stored only an Instant and reconstructed public `due_date` from it.
The analyzer-only evaluation did not exercise this persistence conversion.

## Additive Contract

- `due` retains the existing UTC-normalized Instant contract. A relative date
  leaves it null; no calendar date or timezone is inferred.
- `due_text` is nullable source-supported text, at most 255 Unicode code points.
  Case and Unicode are preserved; surrounding whitespace follows the existing
  analyzer's trimming behavior. Null/blank input produces null.
- The producer repeats the existing source-support guard against the grounded
  action text. Unsupported, non-string or oversized metadata rejects the
  delivery payload with a fixed error, never the rejected value.
- Backend must accept/store nullable `due_text` and expose it as public
  `due_date`, falling back to the legacy Instant only when text is absent.
  Backend deployment precedes producer activation; parent #3753 owns this order.
- Existing redaction, encrypted outbox, tenant/finalization capability and
  idempotency boundaries are unchanged. Backend null-field hash compatibility is
  verified in its paired source work, not inferred from these Python tests.

## Source Verification

Local unit verification: 462 tests passed, 88% app coverage. Focused delivery and
ready-consumer suite: 46 passed. Ruff, Black on changed Python files, mypy (31 app
files), and `git diff --check` passed. One existing Starlette deprecation warning
remains. No model inference, host/config mutation or deployment was performed.

The frozen observed-ASR gold fixture is unchanged:
`7ddd9d4346e7943df8c8c7e30b6d7af1fa4039403d27466a3f08409950af0097`.
Its action date metadata survives the delivery mapping. A separate unit journey
executes ready-consumer -> actual analyzer grounding with a synthetic draft ->
encrypted outbox, checking supported phrase preservation, unsupported-date
rejection, durable plaintext absence, and null Instant behavior.

Only `app/services/analysis_delivery.py` changes among the 31 app files in the
previous inference reports. The other 30 hashes remain identical, including the
analyzer, prompt, model transport and configuration. The old 31-file fingerprint
does not represent this new source head; no new model-evaluation claim is made.

New delivery module SHA256:
`e049f871e506dce3220d497eba6582ee56460aa4a87bb1768784391a21f2cd80`.

Runtime acceptance remains open until parent #3753 verifies a new canonical TEST
recording, persisted/reopened result and unchanged negative authorization gates.
