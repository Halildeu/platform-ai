# Common Voice TR WER Fixture Directory

This directory is the default output target for:

```bash
bash scripts/wer-poc/download-common-voice-tr.sh
```

The script downloads Mozilla Common Voice 17.0 Turkish clips from HuggingFace,
selects a deterministic random sample, writes WAV/TXT pairs, and generates
`ground-truth.json`.

Default output size is 150 clips. Override with environment variables:

```bash
COUNT=200 SEED=20260605 bash scripts/wer-poc/download-common-voice-tr.sh
```

Generated audio and transcript files are not committed by default. They are WER
PoC runtime fixtures and may be large. The source license is CC0 1.0.

PII boundary: do not store Common Voice `client_id`, speaker id, email, raw IP,
or any internal meeting audio here.
