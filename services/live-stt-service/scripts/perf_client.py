"""WER + latency client for the #43 performance matrix.

Stdlib + the pure `wer` module. For every `*.wav` in a fixtures dir that has a
sibling `*.txt` reference, POSTs the audio to /transcribe, computes per-sample
and corpus WER, and aggregates latency-per-audio-minute and the real-time
factor. Prints a JSON summary. GPU/model selection happens in the orchestrator.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import wer


def _post(url: str, wav_path: str, timeout: float) -> tuple[int, dict | None, float]:
    boundary = uuid.uuid4().hex
    with open(wav_path, "rb") as handle:
        data = handle.read()
    filename = os.path.basename(wav_path)
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
    req = urllib.request.Request(url, data=body, method="POST")  # noqa: S310 - local CLI URL
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    start = time.perf_counter()
    status = -1
    payload: dict | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            raw = resp.read()
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:  # noqa: BLE001 - transport failure recorded as status -1
        status = -1
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return status, payload, elapsed_ms


def run(url: str, fixtures_dir: str, pattern: str, timeout: float) -> dict:
    wavs = sorted(glob.glob(os.path.join(fixtures_dir, pattern)))
    samples: list[dict] = []
    pairs: list[tuple[str, str]] = []
    total_audio_s = 0.0
    total_proc_ms = 0.0
    errors = 0

    for wav in wavs:
        txt = os.path.splitext(wav)[0] + ".txt"
        if not os.path.exists(txt):
            continue
        with open(txt, encoding="utf-8") as handle:
            reference = handle.read().strip()

        status, payload, elapsed_ms = _post(url, wav, timeout)
        if status == 200 and payload is not None:
            hypothesis = str(payload.get("text", ""))
            duration = float(payload.get("duration", 0.0))
            proc_ms = float(payload.get("elapsed_ms", elapsed_ms))
            wr = wer.word_error_rate(reference, hypothesis)
            pairs.append((reference, hypothesis))
            total_audio_s += duration
            total_proc_ms += proc_ms
            samples.append(
                {
                    "wav": os.path.basename(wav),
                    "wer": round(float(wr["wer"]), 4),
                    "ref_words": wr["ref_words"],
                    "duration_sec": round(duration, 2),
                    "proc_ms": round(proc_ms, 1),
                }
            )
        else:
            errors += 1
            samples.append({"wav": os.path.basename(wav), "status": status, "ok": False})

    agg = wer.corpus_wer(pairs)
    audio_min = total_audio_s / 60.0
    summary = {
        "samples": len(samples),
        "ok": len(pairs),
        "errors": errors,
        "corpus_wer": round(float(agg["corpus_wer"]), 4),
        "ref_words": agg["ref_words"],
        "total_audio_sec": round(total_audio_s, 2),
        "total_proc_ms": round(total_proc_ms, 1),
        "latency_ms_per_audio_min": (
            round(total_proc_ms / audio_min, 1) if audio_min > 0 else None
        ),
        "realtime_factor": (
            round((total_proc_ms / 1000.0) / total_audio_s, 4) if total_audio_s > 0 else None
        ),
    }
    return {"summary": summary, "samples": samples}


def main() -> int:
    parser = argparse.ArgumentParser(description="#43 WER + latency client")
    parser.add_argument("--url", required=True)
    parser.add_argument("--fixtures-dir", required=True)
    parser.add_argument("--pattern", default="sample-tr-cv17-*.wav")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    result = run(args.url, args.fixtures_dir, args.pattern, args.timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False))  # noqa: T201 - CLI output
    return 0 if result["summary"]["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
