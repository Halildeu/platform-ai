# Failed Smoke Diagnostic Boundary

Source enabler [AI #342](https://github.com/Halildeu/platform-ai/issues/342) supports
[quality #3753](https://github.com/Halildeu/platform-k8s-gitops/issues/3753) and
[Product Slice #3399](https://github.com/Halildeu/platform-k8s-gitops/issues/3399):
deploy the qualified meeting-analysis source to TEST and verify a new persisted
customer journey. This change repairs missing diagnostics, not the unproven
underlying cause of two rollout failures.

## Observed Failure

Candidate `6ebedeebc4cad99c2ff62345f24165e18619a100` was rejected by STT smoke in
[run 34782564438](https://github.com/Halildeu/platform-k8s-gitops/actions/runs/34782564438)
and [run 34783335838](https://github.com/Halildeu/platform-k8s-gitops/actions/runs/34783335838).
Both automatically restored `05a73a32cf510ce7b5afbe591544fa4740770990` and passed
the rollback smokes. The first failure followed a passing first fixture by about
0.4 seconds; the second failed the first fixture. These timings do not establish
a model-quality failure or any specific startup/import/connection cause.

STT and updater sources were unchanged between those revisions. Static inspection
found the exact diagnostic loss: `Invoke-LiveSttFixtureAcceptance` returned on a
nonzero child exit before parsing stdout, discarded stderr, and persisted only
`smoke_exit_code_or_deadline` with a null summary. Existing CLI failure JSON could
therefore not survive into the receipt. Subsequent parent-owned probes on the
restored revision passed both fixtures, including the actual ProcessStartInfo path;
they do not reproduce or erase the forward failures.

## Restricted Projection

The failed-child receipt now adds `failureDiagnostic` with an explicit v1 schema:

- Actual integer exit code and boolean deadline-open state.
- Empty, oversized, invalid, recognized or unrecognized stdout shape; stderr
  presence/oversize shape, never its contents.
- Only the two existing fixed smoke error codes.
- Only a fixed allowlist of exception class names from anchored stderr lines,
  or the fixed `ArgumentParserError` category. This is an observed diagnostic
  classification, not a causal verdict; unknown classes remain null.
- Numeric event/coverage/latency metrics, fixed terminal labels and fixed quality
  failure names from recognized summary JSON. Other values become absent or
  `unrecognized`; URL, transcript, raw errors and arbitrary fields are excluded.

Each stdout/stderr diagnostic input is capped at 65536 characters before parsing.
The existing asynchronous pipe draining remains unchanged. The nonzero exit or
closed-deadline branch still returns false even if stdout claims `ok: true`.
Malformed evidence cannot make acceptance pass. Success acceptance, thresholds,
deadlines, fixture selection, model/prompt and rollback behavior are unchanged.
Original failure receipts remain valid; no new host probe or deployment is part
of this source task.

## Verification

```bash
python -m unittest discover -s tests/unit -p test_gpu_host_update_script.py
```

The existing Windows CI `gpu-host-windows-contract` runs
`tests/windows/test-acceptance-diagnostic.ps1`: safe/unknown/malformed/oversized
projection cases, malicious field exclusion, real protected receipt write/readback,
ACL and no-truthy-output checks, then the actual updater ProcessStartInfo branch
with a synthetic child. Both nonzero-exit and expired-deadline results must remain
boolean false. Windows execution is CI evidence, not local Linux or deployed-host
acceptance. All meeting app hashes must continue matching the four final semantic
reports from PR #341; no additional model inference is required for this isolated
diagnostic change.
