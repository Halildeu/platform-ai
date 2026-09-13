# Meeting Semantic Regression: 2026-09-13

Tracked by platform-k8s-gitops#3753; Product Slice #3399.

Evidence level: synthetic source-service regression, not TEST user acceptance
or measured real-meeting accuracy. The live service was not reconfigured for
these measurements. Only synthetic text was sent to the existing on-prem
Ollama instance through the approved SSH execution route; no external model
provider, new network port, or model download was used.

Baseline source: a7a4841 (unchanged analysis implementation). Candidate source
is identified by the complete before/after application hash manifests in each
report. All reports retain the frozen corpus hash, model digest, prompt hashes,
decoding options and failures. SSH transport overhead is included in elapsed
time; these times are not evidence of the deployed HTTP request deadline.

Selected TEST candidate: `qwen2.5:14b`, digest
`7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6`.
The default model and production configuration are unchanged.

| Report | Decision TP/FP/FN | Action TP/FP/FN | Joint action + metadata TP/FP/FN | Project gate |
| --- | --- | --- | --- | --- |
| baseline | 4/1/5 | 8/5/3 | 4/9/7 | Fail |
| final-gold (12 cases, digest pin enabled) | 9/1/0 | 11/0/0 | 11/0/0 | Pass |
| qwen14-v5-challenge (6 separate cases, digest pin enabled) | 4/0/0 | 9/1/0 | 9/1/0 | Pass |

The predeclared project targets are precision >= 0.90 and recall >= 0.85 for
all three measures, zero execution errors, and stable source/model identities.
These are project thresholds, not a universal industry accuracy standard.
Both final reports have zero errors and stable identities. The gold set still
has one false decision about making no new budget decision; the challenge set
has one extra action in the cancellation case. Neither set is now unseen or
independently human annotated. Earlier failed candidate reports are retained,
not replaced by the successful candidate.

The service rejects an unexpected model digest before inference and withholds
the response if the selected tag differs after inference. This is not an atomic
registry lock. The same guard applies to follow-up questions. Windows runtime
behavior, immutable deployment, authenticated recording, durable reopening,
source navigation and negative authorization require separate exact-head CI
and TEST evidence before customer-delivery claims.
