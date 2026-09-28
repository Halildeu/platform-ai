# Task updates: demonstrated boundaries and proposed replacement

Status: **offline candidate prototype/evidence; not integrated or qualified**.
No STT, deployed model, production prompt, API, runtime, mobile APK or production
behavior changes in this branch. The separate experimental prompt is not shipped.
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

## Earlier boundary checks, 28 September (before executable prototype)

Four boundary tests plus 31 existing live-context/extractive tests pass on local
Python 3.12.10 / pytest 8.3.3 / Pydantic 2.9.2 / pydantic-settings 2.5.2 /
structlog 24.4.0. Selected-module branch/line coverage: live_context 100%,
extractive 96%, combined 97%. This is not whole-service coverage. Ruff and Black
pass. One Starlette multipart deprecation warning remains. Zeyrek is absent;
the existing exact-surface fallback was used. No dependency was installed.

Initial Python 3.10.11 ran the four boundary checks, but could not collect the
existing API tests because that interpreter lacks `datetime.UTC`; the combined
run above used the already installed supported Python 3.12 interpreter. At that
stage model inference and phone acceptance were not run; semantic acceptance remained
FAILED/UNQUALIFIED despite these successful limitation/regression checks.

From the service directory, with `PYTHONPATH=.` and Python 3.12:

```text
python -m pytest experiments/task_state/test_known_limits.py tests/unit/test_live_context.py tests/unit/test_extractive_selection.py -q -o addopts='' --cov=app.services.live_context --cov=app.services.extractive --cov-branch --cov-report=term-missing
python -m ruff check experiments/task_state/test_known_limits.py
python -m black --check experiments/task_state/test_known_limits.py
```

## Executable candidate and preliminary model failure, 28 September

`prototype.py` now executes source-evidenced operations in an **offline candidate
ledger only**. Original source ranges and per-unit/quote hashes are retained.
It applies explicit task patches, keeps cancellations, treats omitted output as
no change, rejects source-revision drift and commits a validated batch atomically.
Same-source reassignment and rescheduling can update disjoint fields, including
across batches; conflicting writes and status changes at that anchor are rejected.

Independent review reproduced three flaws before repair: historical CREATE with
a different description quote could bypass a tombstone, a word/number fragment
could become metadata, and same-anchor disjoint changes were rejected. Four new
tests failed before the fixes and passed afterward. The review channel accepted
the repaired candidate for isolated research, not product integration.

Current offline validation: **70 tests pass** = 25 candidate reducer + 10 probe
safety/metric + 4 known-boundary + 31 existing application regression tests.
Selected branch/line coverage is 89% for the candidate reducer, 72% for the probe,
82% combined. Strict mypy on the prototype passes. The offline experiment tests
are included explicitly in service CI; CI never invokes the model probe.

The full eight-to-seven test uses **manually supplied operations**, not inferred
ones. It verifies retention, Ayşe owning two tasks, Can's tombstone and Zeynep's
date retained with time 11. It does not establish that a real model can produce
those operations. Calendar derivation, canonical replay integration, decisions,
summary, endpoint wiring and phone acceptance remain outside this candidate.

`local-probe-20260928.json` is a **pre-hardening preliminary failure**, bound to
the initial source hashes in that report, not the current code's acceptance run.
Six local Ollama calls yielded three 60-second read timeouts, two invalid-quote
proposals, and one evaluated proposal with a metadata/status mismatch; three
dependent stages were skipped. The eight-task initial stage timed out, so this
run says nothing positive about the 8→7 update. The aggregate mismatch does not
identify which metadata field was wrong; newer probe code reports separate
field mismatch counts for future runs. No raw response was persisted.

Model was local `llama3.1:8b`, digest
`46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`,
Ollama 0.34.4, unchanged before/after. Read-only `/api/ps` reported `size_vram=0`
and context 8192: **CPU execution, not TEST GPU performance**. The five-second
comparison is a local observation, not a TEST acceptance measurement. No model
was installed, pulled, switched in a service, or sent real meeting data.

The preliminary runner checked model fingerprints but not end-of-run code hashes
and continued other cases after a timeout. The current harness now fails on code
drift and stops further inference after a transport timeout, because a client
timeout does not prove the server's work stopped. Those changes have mocked
offline tests; no second model run was performed.

Remaining explicit limitations:

- Exact quote/hash checks prove source integrity, not target/owner entailment.
  Paraphrased duplicate tasks still require qualified semantic reconciliation.
- Historical new CREATE is rejected to prevent old assignments bypassing a
  cancellation. A previously missed task therefore requires explicit complete
  replay/reconciliation; this is not a silent recall-loss solution.
- Unresolved changes accumulate within a revision; clearing a later-resolved
  ambiguity also requires replay. The prototype does not implement that workflow.
- Relative dates remain source phrases (`aynı gün`), never verified calendar
  dates. Reconnect chronology and revision inputs must come from authoritative
  canonical source integration, which is not built here.
- The model's ability to generate complete correct operations, including long
  context and more than ten tasks, remains unqualified. Next semantic work must
  compare relation extraction approaches/models under a verified runtime and
  context budget instead of promoting this failed local experiment.

The missing Saved result requires the separate incomplete-recording lifecycle
repair described above; these operations cannot create a saved meeting result.
