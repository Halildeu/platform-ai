# PR-wer-01 Line #33 Execution Report

Issue: `#33 [PR-wer-01] Common Voice TR sample download script`

## Purpose

Prepare the WER measurement track with a repeatable Common Voice 17 Turkish
fixture downloader. This is data preparation for later WER matrix and model
decision work. It is not a WER result and it does not claim model quality.

## Plan Requirement

The issue asks for:

- `scripts/wer-poc/download-common-voice-tr.sh`
- Mozilla Common Voice 17 Turkish download through HuggingFace datasets API
- 100-200 clip extraction
- random or balanced selection
- output under `tests/fixtures/wer-common-voice-tr/`
- ground truth transcript JSON

## What Changed

- Added root wrapper script:
  - `scripts/wer-poc/download-common-voice-tr.sh`
- Extended the existing reusable downloader:
  - `services/live-stt-service/scripts/download-cv17-tr-samples.py`
- Added fixture target documentation:
  - `tests/fixtures/wer-common-voice-tr/README.md`
- Added fixture output guard:
  - `tests/fixtures/wer-common-voice-tr/.gitignore`
- Generated and committed transcript manifest:
  - `tests/fixtures/wer-common-voice-tr/ground-truth.json`
- Added this execution report:
  - `docs/pr-wer-01-line-33-execution-report.md`

## Behavior

Default command:

```bash
bash scripts/wer-poc/download-common-voice-tr.sh
```

Default output:

```text
tests/fixtures/wer-common-voice-tr/
  cv17-tr-001.wav
  cv17-tr-001.txt
  ...
  ground-truth.json
```

Default sample count is `150`, inside the requested 100-200 range.

The wrapper uses deterministic random reservoir sampling:

```text
COUNT=150
MIN_SEC=3
MAX_SEC=15
SEED=20260605
SCAN_LIMIT=5000
OUT_DIR=tests/fixtures/wer-common-voice-tr
PREFIX=cv17-tr
MANIFEST_JSON=tests/fixtures/wer-common-voice-tr/ground-truth.json
```

## Ground Truth JSON

`ground-truth.json` includes:

- dataset name
- source dataset
- source URL
- license
- locale and split
- output dir
- sample count
- each sample id
- WAV filename
- transcript TXT filename
- sentence
- duration
- sample rate

It intentionally does not store Common Voice `client_id`, speaker id, email, IP,
or any internal meeting metadata.

## Controlled Decisions

| Decision | Reason | Impact |
|---|---|---|
| Reused and extended the existing Python downloader | Avoid duplicate HuggingFace/download logic | Keeps #17 smoke fixture path working |
| Shell wrapper added at root path requested by issue | Issue explicitly requested `scripts/wer-poc/download-common-voice-tr.sh` | Matches plan path |
| Random deterministic sampling instead of demographic balancing | Issue allows random or balanced; demographic fields are not always stable in streaming rows | Reproducible sample set with fixed seed |
| Generated audio not committed | 100-200 WAV files can be large and are runtime fixtures | Script creates them on demand; fixture dir `.gitignore` prevents accidental WAV/TXT commit |
| `ground-truth.json` committed | Issue explicitly asks for ground truth transcript JSON | Keeps transcript manifest reviewable without committing 63 MB audio payload |

## Validation

Executed local validation:

```bash
bash -n scripts/wer-poc/download-common-voice-tr.sh
# PASS

python -m py_compile services/live-stt-service/scripts/download-cv17-tr-samples.py
# PASS

python -m ruff check services/live-stt-service/scripts/download-cv17-tr-samples.py
# PASS

git diff --check
# PASS

python services/live-stt-service/scripts/download-cv17-tr-samples.py --help
# PASS

bash scripts/wer-poc/download-common-voice-tr.sh
# PASS
# Primary dataset `mozilla-foundation/common_voice_17_0` had no streamable data files in this environment.
# Fallback dataset `fsicoli/common_voice_17_0` was used.
# scanned=5000 eligible=4477 selected=150 seed=20260605

python -c "import json; p='tests/fixtures/wer-common-voice-tr/ground-truth.json'; d=json.load(open(p, encoding='utf-8')); print(d['sample_count'], d['source_dataset'], len(d['samples']))"
# PASS -> 150 fsicoli/common_voice_17_0 150
```

Additional compatibility note: local `python` resolves to Python 3.10 on this
PC, so the script intentionally uses `datetime.timezone.utc` instead of the
Python 3.11-only `datetime.UTC` alias.

Network download was run locally. It created 150 WAV files, 150 per-clip TXT
files, and `ground-truth.json` under `tests/fixtures/wer-common-voice-tr/`.
Total local fixture output size was about `63.61 MB`. WAV/TXT files remain
ignored to avoid repository bloat; the generated ground-truth JSON is committed.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| HuggingFace rate limit or auth prompt | Download may fail on first run | `huggingface-cli login` documented |
| Common Voice streaming schema drift | Script may need field adjustment | Manifest stores only stable fields: audio, sentence |
| Large fixture output | Git noise and repo bloat | Generated WAV/TXT files are not committed by default |
| Selection not demographic-balanced | WER sample may be less representative | Later #35/#36 can add matrix/triangulation and balanced dataset rules |

## Alignment

This line stays within `platform-ai` WER preparation scope. It does not touch
GPU code, gateway Redis code, live streaming UX code, or production deployment.

AG-019 staging resource gate pending; implementation validated locally only.
