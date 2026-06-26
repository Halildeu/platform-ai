# Faz 24 AI Gate Evidence Ingest

This runbook describes the source-side, no-mutation ingest path for redacted Faz
24 AI product-gate evidence. It covers G-WER/DER, G-LAT/COST, G-INT, and the
diarization backend decision gate only.

## Boundary

- Do not include raw audio, RTTM files, transcripts, prompts, model responses,
  citation quotes, participant names, email addresses, phone numbers, TCKN-like
  identifiers, IBANs, bearer tokens, API keys, or private keys.
- The workflow uploads only the redacted `result.json`; the submitted input
  envelope is not uploaded as an artifact.
- A `status=pass` result proves only that the selected source verifier accepted
  the submitted redacted evidence under explicit thresholds. It does not enable
  direct-STT, mutate Kubernetes/Vault/firewall state, select a permanent
  model/GPU/LLM provider, choose a permanent diarization backend, or make Faz
  24 production-ready.

## Envelope

Use schema `faz24.ai-gate-ingest.v1` and one of these gate names:

- `gwer`: requires `evidence.werRows`, `evidence.derRows`,
  `thresholds.maxWer`, `thresholds.maxDer`, `thresholds.minWerSamples`,
  `thresholds.minDerSamples`, and `thresholds.minWerRefWords`.
- `glat-cost`: requires `evidence.rows` and explicit G-LAT/COST thresholds.
- `gint`: requires `evidence.rows`, explicit G-INT thresholds, and pilot rows
  with `sha256:<64 hex>` `eval_set_hash`, `prompt_hash`,
  `sample_manifest_hash`, and `sample_count_hash` metadata. G-INT thresholds
  include `minGroundingRate`, `minCitationCoverage`, `minSummaryVerifiedRate`,
  decision/action precision/recall floors, model output-contract ceilings, and
  `minSamples`.
- `diar-decision`: requires `evidence.rows` and explicit diarization decision
  thresholds: `maxDer`, `maxRtf`, `maxLatencyMs`,
  `maxPeakVramDeltaMb`, and `minSamples`. Passing rows also need approved pilot
  evidence, approved license/deployment metadata, `sha256:<64 hex>` evidence
  hash, and explicit `voiceprint_enabled=false`,
  `biometric_processing=false`, and `speaker_identity_mapping=false`.

Example G-WER envelope:

```json
{
  "schema": "faz24.ai-gate-ingest.v1",
  "gate": "gwer",
  "thresholds": {
    "maxWer": 0.25,
    "maxDer": 0.3,
    "minWerSamples": 3,
    "minDerSamples": 3,
    "minWerRefWords": 1000
  },
  "evidence": {
    "werRows": [
      {
        "tag": "pilot-large-v3-turbo",
        "dataset_kind": "pilot-meeting",
        "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "compute": "float16",
        "evidence_hash": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "eval_set_hash": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "sample_manifest_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "sample_count_hash": "sha256:3d01889d9609ee20583304ab0a9bf686a9f83560bdfb4bae3471f1aac9b95120",
        "n_samples": 8,
        "wer": 0.18,
        "ref_words": 1200,
        "ref_word_count_hash": "sha256:2a3a2c703a5e643a184856f3048cd6329a916d04a89ae2e36b69eb51b3573ab8",
        "rtf": 0.08,
        "p50_ms": 420
      }
    ],
    "derRows": [
      {
        "tag": "pilot-pyannote",
        "fixture_kind": "pilot-meeting",
        "backend": "pyannote",
        "model": "pyannote/speaker-diarization-3.1",
        "evidence_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "eval_set_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "sample_manifest_hash": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        "sample_count_hash": "sha256:c6bfbbaa59a6f4994aa14ce8bb723142bef54473ab306f49648e735429e4e54e",
        "n_samples": 8,
        "der_corpus": 0.22,
        "collar": 0.25,
        "rtf": 0.04,
        "p50_ms": 1200
      }
    ]
  }
}
```

Example diarization decision envelope:

```json
{
  "schema": "faz24.ai-gate-ingest.v1",
  "gate": "diar-decision",
  "thresholds": {
    "maxDer": 0.3,
    "maxRtf": 0.15,
    "maxLatencyMs": 2500,
    "maxPeakVramDeltaMb": 4096,
    "minSamples": 8
  },
  "evidence": {
    "rows": [
      {
        "tag": "pilot-pyannote",
        "dataset_kind": "pilot-meeting",
        "backend": "pyannote",
        "model": "pyannote/speaker-diarization-3.1",
        "revision": "abc123",
        "device": "cuda",
        "deployment_mode": "self-host",
        "license_status": "commercial-approved",
        "n_samples": 12,
        "der_corpus": 0.21,
        "der": 0.23,
        "collar": 0.25,
        "skip_overlap": false,
        "lat_max_ms": 1900,
        "rtf": 0.05,
        "peak_vram_delta_mb": 2300,
        "evidence_hash": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        "biometric_processing": false,
        "speaker_identity_mapping": false,
        "voiceprint_enabled": false
      }
    ]
  }
}
```

## Local Use

```bash
python3 scripts/faz24_ai_gate_ingest.py \
  --evidence-file /path/to/redacted-envelope.json \
  --output-file /tmp/faz24-ai-gate-result.json
```

Exit code is `0` only for `status=pass`. `blocked` and `fail` return non-zero
so callers cannot accidentally treat incomplete evidence as accepted.

## GitHub Actions Use

Encode the JSON envelope as a single-line base64 string:

```bash
base64 -w0 redacted-envelope.json
```

Run workflow `Faz 24 AI gate evidence ingest` with that value in
`evidence_json_base64`. The workflow decodes to runner temp storage, invokes the
wrapper, scans only the redacted result artifact, uploads it after scan success,
and fails the run when the gate returns anything other than `pass`.
