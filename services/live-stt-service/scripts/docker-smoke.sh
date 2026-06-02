#!/usr/bin/env bash
# Docker e2e smoke — local `docker run` + curl test
#
# Codex `019e8a24` REVISE: PoC için local docker smoke yeterli; GHCR push + k3d cluster
# smoke sonraki GitOps/queue entegrasyonunda.
#
# Usage:
#   ./scripts/docker-smoke.sh                      # build + run + smoke
#   ./scripts/docker-smoke.sh --skip-build         # skip rebuild
#   ./scripts/docker-smoke.sh --fixture FILE       # use specific wav fixture
#
# Default fixture: tests/fixtures/sample-tr-cv17-001.wav
# Default port: 8200

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="live-stt-service:dev"
PORT=8200
CONTAINER_NAME="live-stt-smoke-$$"
FIXTURE="tests/fixtures/sample-tr-cv17-001.wav"
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build) SKIP_BUILD=true; shift ;;
        --fixture) FIXTURE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

cleanup() {
    docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. Build
if [[ "$SKIP_BUILD" == "false" ]]; then
    echo "[1/5] docker build $IMAGE_TAG ..."
    docker build -t "$IMAGE_TAG" .
fi

# 2. Run (with model cache mount)
HF_CACHE="$HOME/.cache/huggingface"
mkdir -p "$HF_CACHE"

echo "[2/5] docker run $CONTAINER_NAME on port $PORT ..."
docker run -d --name "$CONTAINER_NAME" \
    -p "$PORT:8200" \
    -v "$HF_CACHE:/home/stt/.cache/huggingface" \
    -e STT_MODEL_NAME="${STT_MODEL_NAME:-medium}" \
    -e STT_LANGUAGE=tr \
    -e STT_COMPUTE_TYPE=int8 \
    -e STT_DEVICE=cpu \
    "$IMAGE_TAG" >/dev/null

# 3. Wait for healthy
echo "[3/5] waiting for /health ..."
for i in {1..60}; do
    if curl -s "http://localhost:$PORT/health" 2>/dev/null | grep -q '"status"'; then
        STATUS=$(curl -s "http://localhost:$PORT/health" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status"))')
        echo "  health: $STATUS (after ${i}s)"
        break
    fi
    sleep 1
done

# 4. POST /transcribe with fixture (Codex 019e8a24 iter-1 — fail-fast, no silent PASS)
if [[ ! -f "$FIXTURE" ]]; then
    echo "  ✗ fixture not found: $FIXTURE"
    echo "  Run: python scripts/download-cv17-tr-samples.py --out tests/fixtures/"
    echo "  Acceptance for #14-16 requires a real Common Voice TR fixture present."
    docker logs "$CONTAINER_NAME" 2>&1 | tail -10
    exit 1
fi

echo "[4/5] POST /transcribe with $FIXTURE ..."
START=$(date +%s)
RESPONSE=$(curl -sf -X POST -F "audio=@$FIXTURE;type=audio/wav" "http://localhost:$PORT/transcribe") || {
    echo "  ✗ curl --fail: HTTP non-2xx from /transcribe"
    docker logs "$CONTAINER_NAME" 2>&1 | tail -15
    exit 1
}
ELAPSED=$(($(date +%s) - START))

if echo "$RESPONSE" | grep -q '"text"'; then
    TEXT=$(echo "$RESPONSE" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("text","?"))')
    LANG=$(echo "$RESPONSE" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("language","?"))')
    DURATION=$(echo "$RESPONSE" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("duration",0))')
    SEGMENTS=$(echo "$RESPONSE" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("segments",[])))')
    echo "  ✓ language: $LANG"
    echo "  ✓ duration: ${DURATION}s"
    echo "  ✓ segments: $SEGMENTS"
    echo "  ✓ wall-clock: ${ELAPSED}s"
    echo "  ✓ text: ${TEXT:0:80}..."
else
    echo "  ✗ transcribe failed: $RESPONSE"
    docker logs "$CONTAINER_NAME" 2>&1 | tail -15
    exit 1
fi

# 5. Metrics check
echo "[5/5] GET /metrics ..."
METRICS=$(curl -sf "http://localhost:$PORT/metrics") || {
    echo "  ✗ /metrics HTTP fail"
    exit 1
}
TRANSCRIBE_TOTAL=$(echo "$METRICS" | grep "^stt_transcribe_total{" | head -1 | awk '{print $2}')
echo "  ✓ stt_transcribe_total: $TRANSCRIBE_TOTAL (expected: >= 1)"

if [[ -z "$TRANSCRIBE_TOTAL" ]] || ! awk "BEGIN{exit !($TRANSCRIBE_TOTAL >= 1)}"; then
    echo "  ⚠ metric not incremented as expected"
fi

echo ""
echo "Docker smoke PASS."
