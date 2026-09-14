# Internal speaker adapter preparation (#3746)

Tracked by Halildeu/platform-k8s-gitops#3746, Product Slice #3399.
Blocked customer step: reopen an internal-provider meeting with stable anonymous
speaker attribution, aligned to the persisted transcript. This change alone is
not customer delivery, integrated speaker continuity or runtime acceptance.

## Narrow source repair

`PyannoteDiarizer` now passes a scoped, seekable `BytesIO` to pyannote instead of
writing a temporary WAV. The stream closes on success and inference failure;
the existing single-flight lock and lazy model loading are retained. Configured
`max_speakers` is forwarded to the real pipeline instead of being ignored.
Mock/default backend, model selection, upload API and runtime configuration are
unchanged. Closing the stream is resource release, not a claim of secure wiping
of caller bytes, decoder tensors, allocator memory or operating-system swap.

The pinned [pyannote 3.3.2 input implementation](https://raw.githubusercontent.com/pyannote/pyannote-audio/3.3.2/pyannote/audio/core/io.py)
supports read/seek `IOBase` input. The [3.1 model contract](https://huggingface.co/pyannote/speaker-diarization-3.1)
supports a speaker upper bound without forcing the true number of speakers.

## Verification

- Before the repair, the updated adapter suite had 7 failures and 2 passes.
  The no-temp-file regression rejects the original spool operation.
- After repair: 47 service tests pass, 91% app coverage, 94% adapter coverage;
  Ruff, mypy (13 app files) and Black pass. Tests cover limits 1/3/10/50,
  anonymous annotation conversion, lazy reuse, closure and retry after failure.
- A separate real-library decoder probe ran in the existing isolated CPU
  environment with pyannote.audio 3.3.2 and torchaudio 2.5.1+cpu. All 1,227,136
  decoded mono 16 kHz samples exactly matched the WAV PCM reference; the owned
  stream closed. No model inference or service endpoint was exercised by this
  decoder probe. Evidence SHA-256:
  `77ab57ebf94ab3953ab74d284775c6d6489d7036a1e1cdd91b08b2f09979f1bd`.

## Separate CPU characterization, not acceptance

One predeclared eight-thread, 360-second whole-process measurement used the
unchanged 76.696-second two-voice TTS fixture (SHA-256
`702c3a94e34ca09915237e3fabf11a037602514bb93cec13555ecd3ab7fe2676`).
It used cached model revisions from ADR-0033, CPU-only torch 2.5.1, pyannote.audio
3.3.2 and max_speakers=10. Model access was offline; synthetic audio was supplied
in memory, with no raw-audio file writes in the harness. This was waveform-input
model characterization, not execution of the changed Python service adapter.

Results: 112.500 s inference, 154.990 s whole run, RTF 1.4668, peak working set
2,362,884,096 bytes, two speakers. DER was 4.7541% with a 0.25 s collar and
10.6062% at zero collar, overlap included; confusion was zero. These figures
characterize this synthetic fixture only, not human meetings or noise/overlap
corpus qualification. Earlier 170-second timeouts remain historical unverified
measurements; no retry-until-pass or threshold relaxation was performed.

[Characterization evidence](evidence/internal-speaker-cpu-characterization-2026-09-14.json)
and [real decoder evidence](evidence/internal-speaker-iobase-decoder-2026-09-14.json)
are retained as synthetic, non-acceptance measurements. Characterization SHA-256:
`f54e8e8cb7a77cb40a826bf6838f20e8f74ff9cf2ee17ca33921e6832cb6fc0c`.
Harness SHA-256:
`e975fb90be63dde602c0e9be89e3def29caeae34d15389c0089df3267e5770f8`.
Metric package was pyannote.metrics 4.1 (not the service test pin 3.2.1); no
cross-environment acceptance comparison is claimed. GPU source and ledger
remained `e386b996cae22f08294a83d840f0e92d4a82cd53`, both service readiness
checks passed afterward and no owned probe Python process remained.

## Remaining integration boundary

- CPU speed does not meet the accepted diarization speed budget. Do not insert
  this batch call into live final/EOF handling or extend EOF to hide its cost.
- ADR-0033 post-processing placement and GPU capacity/scheduling still apply.
  No new model was co-loaded on the shared GPU by these measurements.
- ADR-0036 remains authoritative: no raw meeting audio archive is activated.
  The legacy FastAPI `UploadFile` multipart layer can spool before this adapter;
  this PR does not establish a no-raw-persistence HTTP endpoint. Its threadpool
  timeout also does not terminate ongoing model inference.
- Internal STT currently drops word timings. Reusing `final.speakerTurns`
  requires source/text/UTF-16 alignment and session-stable anonymous labels.
  Independent per-window clustering is not continuity. Already committed
  source windows cannot be replayed with changed attribution to overwrite them.
- Bounded transient session state, overlap/unknown handling, measured scheduling
  and a new persisted TEST/browser journey remain under #3746. Existing
  Speechmatics-based #3753 acceptance is preserved, not relabeled as internal.
