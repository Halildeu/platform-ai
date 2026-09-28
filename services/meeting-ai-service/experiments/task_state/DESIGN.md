# Task updates: demonstrated boundaries and proposed replacement

Status: **design/evidence only; not integrated or qualified**. No STT, model,
prompt, API, runtime, mobile APK or production behavior changes in this branch.
Source baseline: `732da87e627a6767eab3d28f014b016f7bcea509`. The application code
matches the earlier punctuation evidence branch `5b110ad`; its failed experiments
remain on that branch and must not be promoted as fixes.

## Three different evidence sets

`reference.json` is the user's synthetic eight-task script and the intended
reassignment, cancellation and deadline update. The final target is seven tasks,
not merely seven displayed entries. The normalized date/time labels are for
evaluation; they are not results returned by the current API.

Its `screenshots` section records user observations: five actions, then two, then
six meaningful tasks plus `11 olacak.`; the year is 2020 instead of the reference
2026. This does not establish what the model actually received, when it ran or
whether the source was already misrecognized. The phone technical report ends
near capture startup; there is no complete stop/reconnect trace.

`test_known_limits.py` independently reproduces four properties of current code
without an LLM or phone. A passing test confirms a LIMITATION, not correct meeting
analysis. Model = NONE. It demonstrates selection-dependent source exclusion
during append-only updates, acceptance of a source-grounded orphan, impossibility of the
correct cross-sentence deadline update, and the ten-action output ceiling.
Prefix changes or an invalid cursor can restore the full menu; exclusion is not
irreversible across every possible request.

## Why another punctuation patch is insufficient

The extractive model can select only a complete source sentence. Its action owner
and due date must be supported by that same sentence. Removing an incorrect dot
cannot generally resolve later reassignment, cancellation or deadline changes.
Copying text proves quotation fidelity, not the semantic correctness of the
selected task or target. The earlier hitap + first-person counterexample remains
mandatory: `Mehmet. Bütçe tablosunu ben kontrol edeceğim.` must not assign Mehmet
merely because his name is nearby or a speaker label matches.

Alternative to qualify: source-evidenced task events and a deterministic task
state, alongside the existing extractive quotations. A stronger relation model
or dedicated relation classifier remains an option if the current qualified
model cannot meet precision/latency requirements. No outside service receives
meeting content for this work. A safe limited fallback is a source-linked change
note marked unresolved, not a fabricated complete task or silent omission.

## Source coverage comes first

Before any 'full source' comparison, establish canonical chronological coverage
across transport epochs, revisions and reconnects. All text in one local buffer
is not automatically the whole meeting. Check the gateway aggregation and
canonical retention independently; a per-bridge accumulator reset alone does
not prove previous backend source was lost. Do not use reset speaker labels as
identity or compare session-local sample offsets as a common timeline.

Read-only review of backend `212fac600f7ca930319241c258a9d216d0068ffd` confirms
`LiveAnalyzeTrigger` maintains a per-meeting cumulative aggregation (bounded to
60,000 characters), independent of the per-bridge accumulator. Canonical ingestion
distinguishes source session, transport epoch and window sequence. Thus reconnect
alone does not establish lost backend history. These are source findings, not a
reconstruction of the missing phone trace or proof of unbounded meeting coverage.

When that source fits a verified model context budget, compare full-source
evaluation with the incremental menu. Model omission must not erase evidence.
When it does not fit, require bounded source retrieval/state reconciliation and
an explicit incomplete-coverage result. No silent truncation, unconditional
union of old tasks, or assumed five-second performance is acceptable.

## Versioned task-state contract proposal

This is a new contract, not extra free text under an old verified citation.

- Stable task IDs refer to logical work. Operation types: create, reassign,
  reschedule, cancel, complete and explicit reopen. Omitting an item is no event.
- Each operation identifies its target and immutable source references including
  transcript revision, source/epoch identity, original ranges and hashes.
- Description, owner, date and time carry separate evidence. Keep the original
  source wording; normalized calendar values additionally record derivation and
  time-zone/reference dependencies. Ambiguous dates remain unresolved.
- A reschedule patches the established task; it never creates a task named
  `11 olacak`. Reassignment changes the owner of that work only. Cancels retain
  a tombstone; a retry or stale answer cannot resurrect cancelled work.
- Apply operations deterministically by canonical source order and revision,
  idempotently. Reject stale, duplicate-conflicting, out-of-scope or mismatched
  evidence. Source corrections invalidate dependent fields and trigger replay.
- Preserve historical values separately. An ambiguous target or unqualified
  relation produces an unresolved change, never a confidently updated task.
- Hash/range validity proves source integrity only. Relation correctness still
  needs independent semantic qualification; it cannot be inferred from locality,
  token overlap or a matching speaker label.

The current source-citation API, backend canonical ingestion, saved analysis,
mobile, exports and retention/erasure must be versioned together before rollout.
Existing consumers cannot interpret multi-source evidence as one original quote.
The incomplete-recording 404 needs a separate terminal lifecycle contract; this
design cannot make a recording complete or a saved result exist.

## Qualification before integration

Use the pinned TEST model/digest and unchanged runtime settings, synthetic data
only initially. Compare source bytes/order/coverage, actual model input/output,
relation rejections and final task sets. Report wrong assignments, missed updates,
false cancellations, orphan tasks, source integrity and latency separately.
Mocked proposals count only as contract tests. Neither fixture identifiers nor
expected owners/dates may enter the model prompt.

The reference 8→7 case must survive omitted intermediate model selections and
long unrelated context. Also cover two tasks for the same owner, same work with
different owners, more than ten tasks, 10→11 vs '11 olabilir'/'11 olmasın', refused
cancellation, historical quotations, hitap + first person, explicit reopen,
duplicate/late deliveries, transcript revisions, reconnects and ambiguous dates.
Cancelled work must remain cancelled; unrelated tasks must not disappear.

No migration, model switch or rollout is authorized by passing the boundary
tests in this folder. Their result is that the current representation is not
sufficient for acceptance.

## Executed checks, 28 September

Four boundary tests plus 31 existing live-context/extractive tests pass on local
Python 3.12.10 / pytest 8.3.3 / Pydantic 2.9.2 / pydantic-settings 2.5.2 /
structlog 24.4.0. Selected-module branch/line coverage: live_context 100%,
extractive 96%, combined 97%. This is not whole-service coverage. Ruff and Black
pass. One Starlette multipart deprecation warning remains. Zeyrek is absent;
the existing exact-surface fallback was used. No dependency was installed.

Initial Python 3.10.11 ran the four boundary checks, but could not collect the
existing API tests because that interpreter lacks `datetime.UTC`; the combined
run above used the already installed supported Python 3.12 interpreter. Model
inference and phone acceptance were not run; the semantic acceptance remains
FAILED/UNQUALIFIED despite these successful limitation/regression checks.

From the service directory, with `PYTHONPATH=.` and Python 3.12:

```text
python -m pytest experiments/task_state/test_known_limits.py tests/unit/test_live_context.py tests/unit/test_extractive_selection.py -q -o addopts='' --cov=app.services.live_context --cov=app.services.extractive --cov-branch --cov-report=term-missing
python -m ruff check experiments/task_state/test_known_limits.py
python -m black --check experiments/task_state/test_known_limits.py
```
