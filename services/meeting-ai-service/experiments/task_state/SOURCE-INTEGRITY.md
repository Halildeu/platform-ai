# Complete-source transport qualification

An incremental cursor currently retains selected old claims and three recent
sentences. An old task omitted by one analysis can therefore disappear from the
next model input, even when the caller supplies its complete original source.
Blindly retaining old output is also unsafe: it can resurrect a cancelled task.

The optional `MAI_OLLAMA_SOURCE_INTEGRITY=true` path retains every canonical source
sentence in its original order, including short context such as a name or numeric
continuation. It ignores the cursor as a source-selection filter. Existing
grounding, metadata validation, source offsets and output contracts still apply.
The cursor remains wire-compatible but cannot delete evidence in this mode.

This flag requires the Ollama backend and `MAI_OLLAMA_EXPECTED_DIGEST`. Before and
after generation, the service checks the tag pin and actual version/architecture.
Initially the only qualified profile is Ollama **0.34.4**, GGUF/llama, digest
`46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`
(the locally installed `llama3.1:8b`). An unknown version/backend/model is rejected,
not assumed compatible. This does not change the default model or enable a rollout.

The request supplies top-level `truncate:false` and `shift:false`. The known
runner rejects an oversized tokenized prompt. Only the matching model's terminal
`done:true`, `done_reason:stop` response can proceed. HTTP errors, missing terminal
metadata, `length`, profile drift and malformed envelopes cannot produce an empty
successful replacement or advance a cursor. There is no heuristic character/token
cutoff or fallback to the pruned source. The same transport checks intentionally
apply to Ask; only analysis has an incremental source menu to bypass.

When no explicit prompt version is supplied, this path reports
`ollama-complete-source-v1`. The feature defaults off. Deployments must qualify
their own exact runtime/model profile before enabling it; older versions are not
admitted merely because upstream source looks similar. Full source costs more
prefill work. A too-large meeting currently receives the existing backend-error
response, not a specialized user-visible capacity state. These are deployment and
product constraints, not a guarantee of full task recall.

## Evidence

`local-source-integrity-r2-20260928.json`: all four synthetic CPU/512-context
transport checks passed in 14.281 seconds with stable model identity:

1. Warm the same model with context shifting enabled; normal terminal success.
2. Switch to shifting disabled; normal terminal success.
3. Supply 2,000 repeated synthetic evidence words; HTTP400 context rejection.
4. Limit generation to one token; terminal `length`, which the application rejects.

The original report is preserved as failed harness evidence: its reused CPU
checker expected context8192, although this probe explicitly requests512. R2
corrected that checker only; the four inputs, options and expected outcomes were
unchanged. This is not semantic task acceptance or a deployed/phone test.

703 offline tests passed, including 23 new source/transport regressions and six
classifier-harness checks. Scoped branch/line coverage: analysis97%, shared
transport100%, integrity95%, combined98%. Ruff and strict mypy (33 app modules)
passed; Black passed for all changed Python files. A broad Black check also found
11 pre-existing unrelated formatting differences, which were left untouched.
Independent review separately ran87 focused tests successfully.

Primary implementation references:
- https://raw.githubusercontent.com/ollama/ollama/v0.34.4/server/sched.go
- https://raw.githubusercontent.com/ollama/ollama/v0.34.4/llm/llama_server.go
- https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/server/server-context.cpp

## Separate semantic experiment

The frozen20-case coarse classifier attempted only `task_event`, `none`, or
`ambiguous` before any task-field extraction. Local CPU Qwen3.5:4B passed the first
five cases, then misclassified a cancellation question as a committed task event.
It stopped immediately after that sixth case. No remaining case, second extraction
layer, prompt tuning or state mutation followed. Its report remains explicitly
unqualified. Preserving source does not repair this semantic error, resolve a split
deadline or change the existing ten-action output cap. No semantic model change is
being proposed as production-ready by this source-integrity patch.
