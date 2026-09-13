# Observed ASR Semantic Regression

Source enabler: [platform-ai#340](https://github.com/Halildeu/platform-ai/issues/340).
Blocked customer step: review and reuse accurate persisted meeting decisions and
action metadata in [Product Slice #3399](https://github.com/Halildeu/platform-k8s-gitops/issues/3399),
tracked by [quality #3753](https://github.com/Halildeu/platform-k8s-gitops/issues/3753).

## Reproduction Boundary

The synthetic TEST recording contained the expected 113 words, but ASR punctuation
and weekday case differed from the original text. The original compound decision
became three source sentences: a decision heading followed by prerequisite
validation and subsequent publication-policy clauses. The canonical source has
112 rows and 19 selectable sentences, not the original 17. Reflowing line breaks
does not restore the original punctuation and must not fabricate a compound quote.

The observed source is independently versioned as
`services/meeting-ai-service/tests/fixtures/meeting-semantic-runtime-asr-v1.json`.
It was annotated and hashed before repair-candidate evaluation:

- Corpus SHA256: `7ddd9d4346e7943df8c8c7e30b6d7af1fa4039403d27466a3f08409950af0097`.
- First case: `two-speaker-tr-v1-observed-asr`, decisions 12/13/17/18,
  actions 10/14/15, owners absent, only action 15 has its same-sentence due phrase.
- Raw synthetic row fixture SHA256:
  `7909d41032351191c01e41010a236d082246fe6ef63989c31f5ea0453b7ab19c`.
- Canonical newline-joined transcript SHA256:
  `b793cffc60d0cc3b56324d0cf82a28459e2d18c158e19476ac653ae175cd01b3`.
- The original corpus SHA256 remains
  `6d09b0dc359d99d0598189cf78104021a0852b688c71fb22102e89f33aa7c1f0`.

Four atomic policy labels in the new source are not an improvement from three to
four original-gold decisions. They represent explicitly different segmentation.
The original strict runtime failure remains valid evidence. On the observed-source
reference that persisted result has decision TP/FP/FN 2/0/2, action 3/0/0, and
action-with-metadata 2/1/1, despite exact source grounding.

## Rejected Prompt Candidate

The measured 14B prompt candidate distinguished a content-free decision heading from its
immediately following concrete adopted policy clauses. Both prerequisite and
subsequent steps belong to the adopted process; lack of the literal decision word
does not make the prerequisite background. Contrast cases keep unaccepted
proposals and historical quotations out of both current decisions and commitments.

Only that candidate's classification prompt changed. Sentence splitting, indices, source
offsets/hashes, exact quote materialization, model/runtime configuration and
fail-closed metadata guards are unchanged. Context may establish adoption; it may
not donate an owner, due date or missing sentence content. Missing metadata is not
filled by guessing a neighboring date.

The candidate recovered all four observed policy facts and all three actions with
metadata in three identical raw-source trials. However, the frozen four-case
observed-source/contrast suite found five false-positive decisions: one extra
heading, two unaccepted proposal clauses and two historical clauses. Its decision
precision was 0.545, failing the unchanged project gate. The original 12-case set
also gained one extra decision (11/12 exact); the six-case challenge gained one
extra action (5/6 exact). These failures are retained in
`docs/evidence/meeting-semantic-runtime-340/`, alongside all raw-source probe
variant scores including unsuccessful variants.

The 14B/prompt combination is rejected, not eligible for deployment. Its 17 prompt
lines were removed before the following independent controls. The frozen fixtures
and regression tests remain useful regardless of model choice. No host
configuration change is part of this source work.

## Rejected Controls

The cached `qwen2.5:32b` control used the original prompt and stable model digest
`9f13ba1299afea09d9a956fc6a85becc99115a6d596fae201a5487a03bdc4368`.
On the four-case observed-source suite, decisions were TP/FP/FN 2/2/4 and actions
with metadata 4/3/0; no case was exact. Case latencies were 104518, 31962, 23969 and
25813 ms. The first case exceeded the existing 60-second per-analysis runtime
budget. Neither accuracy nor runtime suitability was established.

A default-off dense-label experiment required one closed semantic label per
source sentence and exact action-label/metadata consistency before using existing
extractive materialization. Its early four-case suite failed: one schema error,
decision TP/FP/FN 1/4/5, action-with-metadata 1/2/3 and no exact case. A single
raw-source diagnostic did not reproduce the schema error, so no unobserved parser
defect is claimed. It produced valid cardinality and metadata consistency but
misclassified priority status and a bare acknowledgement as decisions, and a
standalone deadline as an action. The actual expected policies/tasks were also
present. Thus structural coverage did not establish semantic classification.

The experimental runtime/config/runner changes were removed, not merged or
enabled. The archived `rejected-dense-post-diagnostic.patch` records the discarded
implementation including later defensive budget/type fixes; it is not represented
as the exact source fingerprint of the earlier dense1 model report. Its archived
unit tests verify contracts, not model quality. No host-facing activation changes
were made for this failed candidate.

The cached `qwen3.8:27b` original-prompt control used stable digest
`22130167c4c20e20c7b71454612966ca8e8171e9b3cc8ab6ce8aa6cbfec79643`
and an explicitly recorded experimental transport override `think: false`.
Both negative context cases were exact, but the actual recording still missed
two policy clauses and the adopted-heading contrast gained one extra decision.
Aggregate decisions were TP/FP/FN 4/1/2; all four actions and metadata matched.
Case times were 56245, 15846, 8841 and 8988 ms. This four-case quality gate also
failed; it did not authorize host activation.

## Qualified Early Control And Canonical Candidate

One predeclared factorial control combined the unchanged frozen 17-line candidate
prompt with the cached `qwen3.8:27b` and explicit `think: false`. All four
observed-source/contrast cases were exact: decisions TP/FP/FN 6/0/0, actions and
joint metadata 4/0/0. Times were 32801, 16325, 11623 and 10492 ms. This control used
explicit experimental in-process prompt/transport overrides, not deployed source.
The prompt hash is
`57f62a473e97abcfd042b842bb7623c969117ce63b32903e661d5f9dcefc7ff6`.
This justified a canonical candidate and broader validation, not customer acceptance.

The canonical candidate restores exactly that frozen prompt and adds nullable
`Settings.ollama_think`, default `None`. Omission preserves the previous request
wire; explicit `False` or `True` is added at the top level of the shared Ollama
generation request used by both analysis and questions. It is not a sampling
option. The upstream API documents this field in
[Generate](https://docs.ollama.com/api/generate).
Existing digest-before/after guards and the 60-second generation budget remain.
Both `/health` and `/ready` expose nonsecret `ollama_think` for Ollama, otherwise null.

The Windows protected configuration contract is:

- `-OllamaThink true` or `false` stores `MAI_OLLAMA_THINK`.
- Omitted or empty parameter preserves the current optional value.
- `-OllamaThink unset` removes the key; `unset` is never a stored value.
- Import clears a stale process value when the key is absent.
- Existing ACL, DPAPI secret storage, backup restore and coupled analysis budgets
  remain covered by the Windows fixture.

Before downgrading to the old source, the current candidate must remove the new
optional key using the controlled unset path (or restore the verified old backup).
Old source rejects unknown config keys, so source-only downgrade with the new key
still present is not a valid rollback. Parent-owned activation must verify the
old model/digest profile, disabled consumers and key absence before source rollback.

Final canonical validation uses the same pinned model, prompt and source hashes,
explicit `think: false` and the unchanged 60-second request budget. The order is
observed-ASR four-case exact gate, original 12-case set, challenge six-case set,
then three exact raw-112-row repetitions. Original gold and thresholds are unchanged.
These repetitions follow other inference and are warm-cache checks; they do not
establish cold-load latency. A new TEST persisted journey remains necessary because
the six-minute upstream finalization minimum can outlast five-minute model keepalive.
The SSH evaluation transport has a 300-second socket timeout, but the canonical
digest-pinned service checks its 60-second deadline after generation; reports also
require measured per-call elapsed time at most 60 seconds. This transport distinction
must not be represented as direct deployed HTTP timeout proof.

### Canonical Source Measurements

All three frozen sets passed with every case exact, not just the aggregate
precision/recall threshold. FP/FN and execution errors were zero in each set:

| Frozen set | Exact cases | Decision TP | Action / joint metadata TP | Max elapsed ms |
| --- | ---: | ---: | ---: | ---: |
| Observed ASR and contrasts | 4/4 | 6 | 4 / 4 | 47809 |
| Original gold | 12/12 | 9 | 11 / 11 | 40266 |
| Challenge | 6/6 | 4 | 9 / 9 | 44345 |

Model digest, app-source hashes and fixture hashes were stable before/after every
set. Actual canonical wire was observed without mutation and asserted to carry
`think: false` and unchanged sampling `{temperature: 0, num_ctx: 8192, top_p: 0.9}`.
No explicit seed or num_predict override was introduced. Ollama version was 0.33.3.

Metadata report SHA256 values:

- `meeting-semantic-340-final-runtime-asr.json`:
  `83468c39f429f236084ef584cbfb734af3341b5f4a6fdcdbd6e03e2586d824b4`.
- `meeting-semantic-340-final-gold.json`:
  `30004c727036e9dc8ee2dd3c2ec5b8f82b80e017b8dbc8f07a2a5d1bafbf6e05`.
- `meeting-semantic-340-final-challenge.json`:
  `2751fb45052880228fd392478ba3362a27b7289cb4bdc2548619e9dce026a5bc`.
- `meeting-semantic-340-final-raw-repeat.json`:
  `9b75ada5fdb702cf744147d6672a45594087708914589cad49c7086daf28eca6`.

All three warm-sequence raw-112-row repetitions were exact: each recovered all
four atomic decisions and three actions with exact owner/date metadata. Elapsed
times were 44361, 43089 and 40821 ms. The final before/after source and model
fingerprints remained unchanged; 25 canonical generation requests were observed.
No additional model inference was needed after this final gate.

The exact metadata-only sequential driver is archived as `final-suite.py.txt`,
SHA256 `654b611ced16a3042cd1e39fbaee3884dc75b0cffb83977b6093faec93eab06a`.
These are visible synthetic regression measurements, not a population accuracy
estimate, speaker diarization score, cold-runtime guarantee or TEST delivery proof.

Local canonical-code verification: 447 unit tests passed with 88% app coverage,
one existing Starlette/AnyIO deprecation warning; mypy passed 31 source files,
service-wide Ruff passed, Black passed the nine changed checked files, and 27
GPU-host update-script unit tests passed. Windows PowerShell/DPAPI fixtures are
not runnable on this Linux DEV host; exact-head Windows CI remains required.

## Verification

From `services/meeting-ai-service` with its Python environment:

```bash
python -m pytest tests/unit --cov=app --cov-report=term --cov-fail-under=85
python -m ruff check app tests/unit/test_semantic_runtime_asr.py
python -m mypy app
python -m black --check app/services/analyze.py tests/unit/test_semantic_runtime_asr.py
MAI_BACKEND=ollama MAI_OLLAMA_MODEL=qwen3.8:27b \
MAI_OLLAMA_EXPECTED_DIGEST=22130167c4c20e20c7b71454612966ca8e8171e9b3cc8ab6ce8aa6cbfec79643 \
MAI_OLLAMA_THINK=false MAI_REQUEST_TIMEOUT=60 \
python scripts/meeting_semantic_eval.py \
  --eval-set tests/fixtures/meeting-semantic-runtime-asr-v1.json \
  --output /tmp/runtime-asr-fresh-report.json
```

Also run the unchanged original 12-case and six-case challenge corpora on the same
candidate. Reports pin effective prompt, source hashes, model fingerprint,
sampling options and fixture hashes. Unit tests use mocked model selections to
verify source/metadata contracts, not model accuracy. Synthetic model checks are
visible regressions once used for tuning, not unseen holdout or human meeting
accuracy. The predeclared project precision/recall gates remain 0.90/0.85; the
observed-source regression additionally requires all four policy facts and all
three actions with exact metadata. TEST persisted browser acceptance and deployment
are separate parent-owned steps; source checks alone do not establish delivery.
