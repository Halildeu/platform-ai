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

python_has_deps() {
  "$1" -c "import datasets, soundfile" >/dev/null 2>&1
}

PYTHON_BIN="${PYTHON:-}"
if [[ -n "${PYTHON_BIN}" ]]; then
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: PYTHON=${PYTHON_BIN} not found." >&2
    exit 1
  fi
  if ! python_has_deps "${PYTHON_BIN}"; then
    echo "ERROR: PYTHON=${PYTHON_BIN} cannot import datasets and soundfile." >&2
    exit 1
  fi
else
  for candidate in python python3 python.exe py.exe; do
    if command -v "${candidate}" >/dev/null 2>&1 && python_has_deps "${candidate}"; then
      PYTHON_BIN="${candidate}"
      break
    fi
  done
  if [[ -z "${PYTHON_BIN}" ]]; then
    echo "ERROR: Python with datasets and soundfile not found." >&2
    echo "Install dependencies or set PYTHON=/path/to/python." >&2
    exit 1
  fi
fi

COUNT="${COUNT:-150}"
MIN_SEC="${MIN_SEC:-3}"
MAX_SEC="${MAX_SEC:-15}"
SEED="${SEED:-20260605}"
SCAN_LIMIT="${SCAN_LIMIT:-5000}"
OUT_DIR="${OUT_DIR:-tests/fixtures/wer-common-voice-tr}"
PREFIX="${PREFIX:-cv17-tr}"
MANIFEST_JSON="${MANIFEST_JSON:-${OUT_DIR}/ground-truth.json}"

cd "${ROOT_DIR}"

PY_SCRIPT_ARG="${PY_SCRIPT}"
OUT_ARG="${OUT_DIR}"
MANIFEST_ARG="${MANIFEST_JSON}"
if [[ "${PYTHON_BIN}" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
  PY_SCRIPT_ARG="$(wslpath -w "${PY_SCRIPT}")"
  OUT_ARG="$(wslpath -w "${ROOT_DIR}/${OUT_DIR}")"
  MANIFEST_ARG="$(wslpath -w "${ROOT_DIR}/${MANIFEST_JSON}")"
fi

"${PYTHON_BIN}" "${PY_SCRIPT_ARG}" \
  --count "${COUNT}" \
  --min-sec "${MIN_SEC}" \
  --max-sec "${MAX_SEC}" \
  --selection random \
  --seed "${SEED}" \
  --scan-limit "${SCAN_LIMIT}" \
  --prefix "${PREFIX}" \
  --out "${OUT_ARG}" \
  --manifest-json "${MANIFEST_ARG}"
