# Faz 24 AI Gate Evidence Ingest

This runbook describes the source-side, no-mutation ingest path for redacted Faz
24 AI product-gate evidence. It covers G-WER/DER, G-LAT/COST, and G-INT only.

## Boundary

- Do not include raw audio, RTTM files, transcripts, prompts, model responses,
  citation quotes, participant names, email addresses, phone numbers, TCKN-like
  identifiers, IBANs, bearer tokens, API keys, or private keys.
- The workflow uploads only the redacted `result.json`; the submitted input
  envelope is not uploaded as an artifact.
- A `status=pass` result proves only that the selected source verifier accepted
  the submitted redacted evidence under explicit thresholds. It does not enable
  direct-STT, mutate Kubernetes/Vault/firewall state, select a permanent
  model/GPU/LLM provider, or make Faz 24 production-ready.

## Envelope

Use schema `faz24.ai-gate-ingest.v1` and one of these gate names:

- `gwer`: requires `evidence.werRows`, `evidence.derRows`,
  `thresholds.maxWer`, and `thresholds.maxDer`.
- `glat-cost`: requires `evidence.rows` and explicit G-LAT/COST thresholds.
- `gint`: requires `evidence.rows` and explicit G-INT thresholds.

Example G-WER envelope:

```json
{
  "schema": "faz24.ai-gate-ingest.v1",
  "gate": "gwer",
  "thresholds": {
    "maxWer": 0.25,
    "maxDer": 0.3
  },
  "evidence": {
    "werRows": [
      {
        "tag": "pilot-large-v3-turbo",
        "dataset_kind": "pilot-meeting",
        "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "compute": "float16",
        "n_samples": 8,
        "wer": 0.18,
        "ref_words": 1200,
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
