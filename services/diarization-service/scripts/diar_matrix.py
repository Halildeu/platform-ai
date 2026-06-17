"""#161 (T-B) diarization matrix runner — DER + VRAM + RTF per backend.

Pure-measurement tool (the diarization sibling of live-stt's wer_matrix.py).
Runs a diarization backend over an audio set, scores DER against RTTM
ground-truth, and reports one JSON row per run — feeding the ADR-0033 decision
with EVIDENCE (önce ölç, sonra karar: WER #35 → ADR-0031 disiplini).

Backends (ADR-0033 candidates): `pyannote` wired now; `nemo` / `speechbrain`
are stubs that raise a clear message until their adapter lands (ölçülmeden
kilitlenmez — aynı harness üçünü de yarıştırır).

Ground-truth: RTTM (diarization standard). Reference resolution per audio:
  `<stem>.rttm` next to the wav.

DER scoring uses pyannote.metrics (Hungarian speaker mapping) — anonymous
SPEAKER_xx labels in hyp/ref are optimally matched, so label numbering differs
harmlessly. No voiceprint/embedding is stored (ADR-0030 boundary).

Usage (GPU host, HF token for gated pyannote model):
  DIA_HF_TOKEN=hf_xxx python scripts/diar_matrix.py --backend pyannote \
      --model pyannote/speaker-diarization-3.1 --device cuda \
      --audio-dir tests/fixtures/diar-tr --tag pyannote-3.1
JSON row to stdout; human summary to stderr (sweeps redirect stdout to a file).
"""
# ruff: noqa: T201, S603, S607 - measurement CLI: prints are the output;
# nvidia-smi is a fixed-arg trusted binary (same pattern as wer_matrix.py).

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import wave as wave_mod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Turn:
    """One speaker turn: [start, end) seconds, anonymous label."""

    start: float
    end: float
    speaker: str


def _wav_duration(path: Path) -> float:
    try:
        with wave_mod.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else 0.0
    except (wave_mod.Error, EOFError, OSError):
        return 0.0


def load_rttm(path: Path) -> list[Turn]:
    """Parse RTTM SPEAKER lines: 'SPEAKER file 1 start dur <NA> <NA> spk <NA> <NA>'."""
    turns: list[Turn] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        start, dur = float(parts[3]), float(parts[4])
        turns.append(Turn(start=start, end=start + dur, speaker=parts[7]))
    return turns


def compute_der(reference: list[Turn], hypothesis: list[Turn]) -> float:
    """DER via pyannote.metrics (optimal speaker mapping). Lower is better."""
    from pyannote.core import Annotation, Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    ref = Annotation()
    for t in reference:
        ref[Segment(t.start, t.end)] = t.speaker
    hyp = Annotation()
    for t in hypothesis:
        hyp[Segment(t.start, t.end)] = t.speaker
    return float(DiarizationErrorRate()(ref, hyp))


def _vram_mb() -> int:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    )
    return int(out.decode().splitlines()[0].strip())


def run_pyannote(model: str, device: str, token: str) -> tuple[object, float]:
    """Load pyannote pipeline; return (pipeline, load_sec). Heavy import deferred."""
    import torch  # type: ignore[import-not-found]
    from pyannote.audio import Pipeline

    # pyannote 3.x checkpoints predate torch 2.6's weights_only=True default.
    # The official pyannote weights are a trusted source, so load with
    # weights_only=False just for the pipeline load, then restore the default.
    _orig_load = torch.load

    def _trusted_load(*a: object, **k: object) -> object:
        # Force-override: lightning passes weights_only=True explicitly, so
        # setdefault is not enough — pyannote weights are trusted.
        k["weights_only"] = False
        return _orig_load(*a, **k)

    torch.load = _trusted_load  # type: ignore[assignment]
    try:
        t0 = time.perf_counter()
        pipeline = Pipeline.from_pretrained(model, use_auth_token=token)
    finally:
        torch.load = _orig_load  # type: ignore[assignment]
    if device == "cuda":
        pipeline.to(torch.device("cuda"))
    return pipeline, time.perf_counter() - t0


def _pyannote_turns(pipeline: object, wav: Path) -> list[Turn]:
    annotation = pipeline(str(wav))  # type: ignore[operator]
    return [
        Turn(start=float(seg.start), end=float(seg.end), speaker=str(label))
        for seg, _, label in annotation.itertracks(yield_label=True)
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="pyannote", choices=["pyannote", "nemo", "speechbrain"])
    ap.add_argument("--model", default="pyannote/speaker-diarization-3.1")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--audio-dir", default="tests/fixtures/diar-tr")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--tag", default="", help="label for the JSON result row")
    args = ap.parse_args()

    if args.backend in {"nemo", "speechbrain"}:
        print(
            f"backend '{args.backend}' adapter not wired yet — ADR-0033 candidate; "
            "pyannote ölçülüp baseline kurulduktan sonra eklenecek.",
            file=sys.stderr,
        )
        sys.exit(3)

    audio_dir = Path(args.audio_dir)
    wavs = sorted(w for w in audio_dir.glob("*.wav") if w.with_suffix(".rttm").exists())
    if args.limit > 0:
        wavs = wavs[: args.limit]
    if not wavs:
        print(f"NO wav+rttm pairs in {audio_dir}", file=sys.stderr)
        sys.exit(2)

    peak = {"mb": 0}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            try:
                peak["mb"] = max(peak["mb"], _vram_mb())
            except Exception:  # noqa: BLE001
                return  # no nvidia-smi (CPU run) — stop sampling
            time.sleep(0.2)

    threading.Thread(target=sampler, daemon=True).start()

    token = os.getenv("DIA_HF_TOKEN", "")
    if not token:
        print("DIA_HF_TOKEN not set — gated pyannote model needs it", file=sys.stderr)
        sys.exit(4)

    pipeline, load_sec = run_pyannote(args.model, args.device, token)

    ders: list[float] = []
    latencies: list[float] = []
    audio_sec_total = 0.0
    for wav in wavs:
        ref = load_rttm(wav.with_suffix(".rttm"))
        t0 = time.perf_counter()
        hyp = _pyannote_turns(pipeline, wav)
        latencies.append((time.perf_counter() - t0) * 1000)
        audio_sec_total += _wav_duration(wav)
        ders.append(compute_der(ref, hyp))
        print(f"  [{len(ders)}/{len(wavs)}] {wav.name} DER={ders[-1]:.3f}", file=sys.stderr)

    stop.set()
    proc_sec = sum(latencies) / 1000.0
    lat_sorted = sorted(latencies)
    p95 = lat_sorted[min(len(lat_sorted) - 1, int(0.95 * len(lat_sorted)))]
    row = {
        "tag": args.tag or f"{args.backend}",
        "backend": args.backend,
        "model": args.model,
        "device": args.device,
        "n_samples": len(ders),
        "der": round(statistics.mean(ders), 4),
        "der_p95": round(sorted(ders)[min(len(ders) - 1, int(0.95 * len(ders)))], 4),
        "p50_ms": round(statistics.median(latencies)),
        "p95_ms": round(p95),
        "audio_sec": round(audio_sec_total, 1),
        "rtf": round(proc_sec / audio_sec_total, 3) if audio_sec_total else None,
        "model_load_sec": round(load_sec, 1),
        "peak_vram_mb": peak["mb"],
    }
    print(json.dumps(row, ensure_ascii=False))
    print(
        f"== {row['tag']}: DER {row['der']:.2%} on {row['n_samples']} samples, "
        f"p50 {row['p50_ms']}ms, RTF {row['rtf']}, peak VRAM {row['peak_vram_mb']}MB",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
