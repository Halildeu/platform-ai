"""Concurrent /transcribe load client for the #42 saturation harness.

Stdlib only (urllib + threads) so it runs in any Python on the GPU host without
extra installs. Fires `--concurrency` simultaneous multipart POSTs of one audio
fixture and prints a JSON summary (latency percentiles, throughput, overlap,
errors) plus raw per-request records.

Usage:
    python saturation_client.py --url http://127.0.0.1:18220/transcribe \
        --audio tests/fixtures/sample-tr-cv17-001.wav --concurrency 4 --timeout 360
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import saturation_stats as ss


def _build_multipart(filepath: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    with open(filepath, "rb") as handle:
        data = handle.read()
    filename = os.path.basename(filepath)
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="audio"; ' f'filename="{filename}"\r\n'
            ).encode(),
            b"Content-Type: audio/wav\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, boundary


def _post_once(url: str, body: bytes, boundary: str, timeout: float) -> dict:
    req = urllib.request.Request(url, data=body, method="POST")  # noqa: S310 - local http URL from CLI arg
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    start = time.perf_counter()
    status = -1
    ok = False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            resp.read()
        ok = 200 <= status < 300
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:  # noqa: BLE001 - transport failure recorded as status -1
        status = -1
    end = time.perf_counter()
    return {
        "start": start,
        "end": end,
        "elapsed_ms": round((end - start) * 1000.0, 1),
        "status": status,
        "ok": ok,
    }


def run(url: str, audio: str, concurrency: int, timeout: float) -> dict:
    body, boundary = _build_multipart(audio)

    def task(_: int) -> dict:
        return _post_once(url, body, boundary, timeout)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        records = list(pool.map(task, range(concurrency)))

    summary = ss.summarize(records)
    summary["concurrency"] = concurrency
    return {"summary": summary, "records": records}


def main() -> int:
    parser = argparse.ArgumentParser(description="#42 saturation load client")
    parser.add_argument("--url", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=360.0)
    args = parser.parse_args()

    result = run(args.url, args.audio, args.concurrency, args.timeout)
    print(json.dumps(result, indent=2))
    # Non-zero exit if every request failed, so the orchestrator can react.
    return 0 if result["summary"]["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
