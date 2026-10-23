# Task updates: demonstrated boundaries and proposed replacement

Status: **offline task-state candidate; not integrated or qualified**.
A separate, narrow application change now withholds standalone numeric copular
fragments such as `11 olacak.` from analysis publication. It neither resolves a
task reference nor updates the previous deadline. The source, model input, general
citation/Ask behavior and task-state integration remain unchanged. No deployed
model, STT, mobile APK or server changes were made for this local repair.
Source baseline: `732da87e627a6767eab3d28f014b016f7bcea509`. The application code
previously matched the punctuation evidence branch `5b110ad`; those failed
experiments must not be promoted as fixes.

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

`test_known_limits.py` independently reproduces four representation properties
without an LLM or phone. A passing test confirms a LIMITATION, not correct meeting
analysis. Model = NONE. It demonstrates selection-dependent source exclusion
during append-only updates, quotation grounding of an orphan, impossibility of the
correct cross-sentence deadline update, and the ten-action output ceiling.
Prefix changes or an invalid cursor can restore the full menu; exclusion is not
irreversible across every possible request.
The final analysis-specific completeness guard now withholds a numeric orphan
even when its quotation passes. That narrower repair is tested separately in
`tests/unit/test_analysis_fragment_guard.py`; the raw source is retained as context.

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

## Indexed evidence candidate, 28 September

`indexed.py` reduces the model's copying burden: it selects exact numbered token
ranges and request-local numeric task aliases. The decoder slices the original
characters, validates word boundaries and the selected occurrence, and binds the
proposal to both the source and prior ledger hashes. It rejects forward targets,
duplicate creates and out-of-order operations before the atomic ledger update.
An exact span is still not proof of a correct owner or task relation.

`indexed_probe.py --mode incremental` retains the previous candidate state and
skips dependent stages after a failed relation. `--mode replay` independently
rebuilds each stage from the complete provided source. Replay returns a fresh
candidate; it never replaces stored state or treats omitted output as cancellation.
Canonical replay, reconciliation and approval of a candidate are not implemented.
Only synthetic local-loopback inputs and already-installed pinned models are used.

`local-qwen-indexed-20260928.json` is an unchanged **failed preliminary incremental
experiment**, bound to the code hashes inside it. With local Qwen 2.5 3B Instruct,
the first eight-task stage reached 180.047 seconds without an observed generation
completion. No task relation was evaluated and no further inference was queued.
Model digest `357c53fb659c5076de1d65ccb0b397446227b71a42be9d1603d46168015c9e4b`
and Ollama 0.34.4 were stable. Reported VRAM usage was zero; this is not TEST GPU
latency or evidence of the model's semantic accuracy. A client deadline does not
prove that server-side generation stopped.

That experiment predates removal of a specific time-correction example from the
prompt and introduction of explicit incremental/replay modes. It is **not a
qualification run of the final source**. Replay recovery is proven only with
manually supplied or mocked proposals; there was no second real-model run.

Final offline verification: **109 tests pass**, including 29 indexed-evidence
and 10 indexed-probe tests plus the earlier 70 checks. Scoped branch/line coverage:
indexed decoder 97%, indexed probe 80%, reducer 89%, combined 88%. Ruff and Black
pass. Strict mypy passes on the reducer and decoder using explicit package bases
and silent imported-module checking. Independent final review accepted research
scope only and rechecked the final ten probe tests. No application source,
production prompt, service model, recording path or APK changes in this delta.

Relation extraction, chronology across reconnect, normalized dates, decisions,
summary and real-device acceptance remain unqualified. A qualified relation
extractor/model and an observed evaluation runtime are required before integration.

## Local publication repair and alternative evaluation, 28 September

The application-specific grounding guard withholds standalone numeric continuations
from actions, decisions and summary. `11 olacak.` remains in original source and
model context, but is rejected with `context_dependent_numeric_fragment` instead
of appearing as a complete task. Generic citation/Ask, offsets and existing API
contracts remain unchanged. This does **not** resolve the target, update a deadline,
prevent omitted tasks or repair recording finalization. There is no server rollout.

The separate compact candidate maps short JSON fields into the unchanged indexed
evidence/reducer pipeline. Its generated JSON schema enforces operation-specific
shapes, including required work for creates and date/time for rescheduling. Exact
spans and valid shapes still do not establish correct task relationships.

Three bounded local runs are preserved as failed evidence:

| Report | First reference case result | Local CPU seconds |
| --- | --- | ---: |
| `local-qwen-compact-20260928.json` | Qwen2.5:3B output-length limit; no relation evaluation | 94.766 |
| `local-qwen35-compact-20260928.json` | Qwen3.5:4B completed, but violated create contract | 63.125 |
| `local-qwen35-compact-shaped-20260928.json` | Operation-shaped schema; no observed completion by deadline | 120.063 |

The last run is the single reviewed retry after fixing the generated schema.
Its prompt, frozen expected results and reducer were unchanged. Every run stopped
at its first failure; no later case or partial candidate state was accepted. A
client deadline does not prove server-side generation stopped. No further call
was queued. Each report identifies its historical code hashes; only the last
report describes the final adapter. None qualifies extraction accuracy or speed.

Qwen3.5:4B was downloaded from the official Ollama registry for this CPU-only
synthetic experiment. Manifest/model digest is
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`.
The existing local Ollama 0.34.4 and deployed defaults were unchanged. Context was
8192, output cap 1024, temperature 0, seed 42, `think=false`, `num_gpu=0`.
This is not TEST GPU performance and no real meeting content went to external
inference. Model/code fingerprints stayed stable within the runs.

Final offline verification: **626 unit and experiment tests pass** on Python
3.12.10 using `-X utf8`; analysis module coverage 97%, compact decoder 100%,
compact probe 75% (combined scoped branch/line coverage 92%). Ruff, Black and
strict mypy for the changed application module and adapter pass. The 27 new
publication regressions cover live/final output, indexed and legacy model paths,
valid neighboring claims, source fidelity and rejection reasons. Nine negative
operation-shape tests failed before the schema repair and now pass. Model replies
in these unit tests are controlled fixtures, not semantic acceptance evidence.

On Windows, three pre-existing hash-frozen JSON fixtures needed exact Git HEAD
bytes instead of autocrlf-expanded bytes, and UTF-8 decoding. Their content and
expected hashes were not changed. The initial five baseline failures reproduced
with the original application code and disappeared after this QA correction.
One existing Starlette multipart deprecation warning remains.

Independent final review accepted this narrow application change and isolated
experiment only, and reran 68 focused tests successfully. It verified the five
generated schema variants and final report/code hashes. This does not qualify
task recall, the real-model 8-to-7 transition, live source coverage or the phone.
The newly loaded experimental model was unloaded afterward; runtime confirmed
`done_reason=unload`. Its downloaded files are retained, and no default changed.

## Sequential extraction and bounded local transport, 28 September

The isolated `stepped.py` candidate processes one new canonical source unit with
at most two preceding context units and prior task state. Code owns positions
and task aliases; proposals must quote exactly within an explicitly selected
scope. Operation-specific schemas require the fields needed for each operation.
When no preceding unit exists, context_fields must be empty. This concrete schema
defect was reproduced before repair; strict evidence matching was not relaxed.

The probe now embeds the output schema in the prompt as well as the structured
format. It records only hashes and bounded diagnostic metadata, never raw model
replies. Quote diagnostics distinguish missing, ambiguous, boundary-only and
wrong-scope evidence without accepting or automatically repairing it.

`bounded_inference.py` is loopback-only experimental transport. An asynchronous
deadline includes connection and the complete streamed response. Request and
response size caps apply even to a stream without line breaks. Only a matching
model with an observed successful terminal event can return a result; external
cancellation propagates. None of this establishes that server work stopped.

Preserved real-model runs are all **failed/unqualified**:

| Report | Observed result |
| --- | --- |
| local-qwen35-stepped-20260928.json | Two explicit tasks both returned no_event |
| local-llama-stepped-smoke-20260928.json | Missing task, then invalid quotation; detailed cause not recorded |
| local-llama-stepped-schema-20260928.json | First proposal rejected for quotation scope |
| local-llama-quote-diagnostic-20260928.json | Identical first response hash; exact current quotes selected nonexistent previous scope |
| local-llama-stepped-scope-20260928.json | First task accepted after schema repair; second call hit a 30-second read timeout |
| local-llama-stepped-bounded-20260928.json | Completed both calls, but only one of two tasks matched; second returned no_event |

The final run used pinned local Llama3.1:8B, CPU-only, context8192, output384,
temperature0, seed42 and top_p0.9. The calls took 33.250 and 55.781 seconds; code
and model hashes remained stable. No later case ran. Historical reports retain
their own code hashes and are not reclassified as qualification of later code.
The smoke inputs and frozen expected states were unchanged. No experiment is
imported by the application, and no model/default/runtime/phone change follows.

Sequential pending corrections remain a separate unresolved design issue:
`10 değil.` may require the next unit before a defensible update exists. Current
unresolved markers accumulate and the runner stops. Bounded replay from an
accepted checkpoint and explicit pending resolution must be designed before
integration; it would not itself repair the independently observed missed task.

Final offline verification: 674 unit/experiment tests pass in 17.70 seconds.
Scoped branch/line coverage is analyze97%, stepped89%, stepped_probe85%,
quote_diagnostics100%, bounded_inference80%, combined91%. Ruff/Black and scoped
strict mypy pass. Independent review reran 18 transport/probe tests and accepted
the experimental transport only. Successful controlled tests do not establish
semantic accuracy; all real-model qualification and phone acceptance flags remain
false. Historical TEST uses a different model; its current identity is unverified.
