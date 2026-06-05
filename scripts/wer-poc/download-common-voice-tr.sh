#!/usr/bin/env bash
set -euo pipefail

# Download a privacy-safe Mozilla Common Voice 17 Turkish WER fixture set.
#
# Defaults are intentionally WER-sized. Override with env vars:
#   COUNT=150
#   MIN_SEC=3
#   MAX_SEC=15
#   SEED=20260605
#   SCAN_LIMIT=5000
#   OUT_DIR=tests/fixtures/wer-common-voice-tr
#
# Prerequisites:
#   pip install datasets soundfile
#   huggingface-cli login   # optional, helps avoid HF rate limits

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_SCRIPT="${ROOT_DIR}/services/live-stt-service/scripts/download-cv17-tr-samples.py"

COUNT="${COUNT:-150}"
MIN_SEC="${MIN_SEC:-3}"
MAX_SEC="${MAX_SEC:-15}"
SEED="${SEED:-20260605}"
SCAN_LIMIT="${SCAN_LIMIT:-5000}"
OUT_DIR="${OUT_DIR:-tests/fixtures/wer-common-voice-tr}"
PREFIX="${PREFIX:-cv17-tr}"
MANIFEST_JSON="${MANIFEST_JSON:-${OUT_DIR}/ground-truth.json}"

cd "${ROOT_DIR}"

python "${PY_SCRIPT}" \
  --count "${COUNT}" \
  --min-sec "${MIN_SEC}" \
  --max-sec "${MAX_SEC}" \
  --selection random \
  --seed "${SEED}" \
  --scan-limit "${SCAN_LIMIT}" \
  --prefix "${PREFIX}" \
  --out "${OUT_DIR}" \
  --manifest-json "${MANIFEST_JSON}"
