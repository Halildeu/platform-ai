# Semantic Regression Evaluation

Tracked by platform-k8s-gitops#3753; Product Slice #3399. This check supports the
customer step of reopening a meeting and using correctly classified decisions
and assigned work. It does not replace the authenticated TEST recording,
durable-result, citation-navigation, or authorization-negative acceptance flow.

## Labels And Metrics

`tests/fixtures/meeting-semantic-gold-v1.json` contains 12 synthetic Turkish
cases, including mixed English terminology and the existing two-speaker audio
fixture transcript. Definitions and rationale are stored with the annotations.
The fixture was frozen before measuring the candidate prompt. It is a small
visible regression set, not unseen holdout data, a human-annotated meeting
benchmark, or evidence of population accuracy.

- A decision is an explicit adopted concrete choice, including rejection,
  no-change policy and an agreed contingency. A historical report, question,
  unaccepted proposal or unspecified acknowledgement is not a decision.
- An action is a concrete outstanding task explicitly assigned or accepted.
  A completed task, wish or untriggered general policy is not an action.
- Owners and due phrases must be explicit in the same source sentence. A
  first-person or anonymous speaker label does not establish a named owner.
- A predicted sentence must equal its gold source sentence exactly. Each gold
  label can match once: duplicate predictions count as false positives. Shared
  vocabulary and token overlap do not earn semantic-classification credit.
- Decision and action precision, recall and F1 use TP/FP/FN micro-counts. Empty
  gold plus empty prediction is a correct negative case, but many such cases
  cannot inflate positive micro-counts. Execution errors are separately counted
  and always fail the project gate, even on a case with no expected labels.
- Action text classification, owner/date equality and the joint action plus
  metadata result are reported separately. Exact source grounding is independent:
  copying a past-status sentence as a decision can yield grounding 1.0 and
  semantic decision precision 0.0.

The current project regression targets are precision at least 0.90 and recall
at least 0.85 for decisions, actions and joint actions with metadata, with zero
execution errors and stable source/model fingerprints. These are explicit
project targets, not an industry-standard accuracy claim. Keep failed results;
do not change annotations to make a candidate pass. Add separately versioned
cases when the classification contract changes.

## Run

From the meeting-ai-service directory, with its development dependencies:

```bash
python -m pytest tests/unit/test_semantic_eval.py tests/unit/test_semantic_eval_runner.py --cov=app.services.semantic_eval --cov=meeting_semantic_eval --cov-report=term-missing
MAI_BACKEND=ollama MAI_REQUEST_TIMEOUT=300 python scripts/meeting_semantic_eval.py --eval-set tests/fixtures/meeting-semantic-gold-v1.json --output /tmp/semantic-candidate.json
```

Use the deployed on-prem Ollama configuration through the approved network
route. Redaction remains enabled. The runner rejects mock mode and never falls
back to another provider. Use one model run at a time on the shared GPU.

The runner invokes the real `MeetingAnalysisService`, not only a raw model
prompt. stdout and optional output contain metadata-only JSON; stderr progress
contains case ID, elapsed milliseconds and error type. Reports include fixture
SHA-256, source-file hashes before and after, extractive and legacy prompt
hashes, decoding options, Ollama version and actual model digest before and
after. A changed or missing fingerprint cannot pass. Exit code 0 means only
the declared synthetic regression gate passed; exit code 1 preserves a failed
report. It does not mean TEST user acceptance or production readiness.

For comparison, use the same frozen fixture and decoding options with separate
baseline and candidate source checkouts and distinct report paths. Record the
exact source commits alongside these reports. Improvement on this visible set
must not be described as general Turkish meeting quality or human speaker
identification accuracy.
