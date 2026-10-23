# Canonical recording outcome compatibility

Companion to platform-backend PR1177's immutable snapshot and signed capability
contract. This reader change must be deployed before transcript-service adds
`recordingOutcome` and `recordingIncompleteReason` to its canonical snapshot DTO.

Old responses omitting both fields remain readable with UNKNOWN provenance. New
responses allow only UNKNOWN/FINISHED without a reason, or INCOMPLETE with the
bounded reason CLOSURE_UNCONFIRMED. Null outcome, numeric ordinals, unknown enum
values, orphan reasons and conflicting aliases are invalid responses. Existing
extra-field rejection, content hash, segment, tenant and exact occurrence checks
remain in force. Validation errors contain no response or transcript content.

FINISHED means the canonical recording-finished event was observed; it does not
independently prove that every sound sample was captured. Analysis of retained
content from an incomplete recording is allowed. These markers do not enter model
prompts or the AI-authored ingestion body. Meeting-service persists the pair from
the capability signed by transcript-service for the same immutable occurrence.

Roll out the compatible AI reader and meeting verifier/writer before enabling the
new transcript response/issuer. Backend producer/consumer ordering, client labels,
empty/failed outcome handling and actual recording acceptance are separate required
gates. This compatibility patch does not fix task reassignment, cancellation, date
extraction, or the user's missing historical saved result.
