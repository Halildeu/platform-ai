"""#35 WER matrix runner — transcribe a fixture set and report WER + latency.

Feeds the #35/#36/#43 matrix. Pure-measurement tool: loads a faster-whisper
model directly (no service stack), transcribes every WAV in --samples-dir and
scores against references using scripts/wer.py.

Reference resolution per sample (first hit wins):
  1. `<stem>.txt` next to the wav (GPU-PC downloaded fixture layout)
  2. `ground-truth.json` manifest entry (--manifest), matching by file name

Usage (GPU host):
  python scripts/wer_matrix.py --model medium --compute float16 --device cuda \
      --samples-dir tests/fixtures/wer-cv17-tr --limit 50 --tag medium-fp16
Outputs one JSON line per run to stdout + a human summary to stderr, so sweeps
can be redirected into a results file.
"""
# ruff: noqa: T201, S603, S607 - measurement CLI: prints are the output;
# nvidia-smi is a fixed-arg trusted binary (same pattern as measure_shared.py).

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import wave as wave_mod
from pathlib import Path

from cost import (  # same scripts/ dir — #161 cost/dakika (DRY, reuses #43 model)
    audio_minutes_per_wall_hour,
    cost_per_audio_minute,
    local_cost_per_hour,
)
from wer import corpus_wer  # same scripts/ dir


def _cost_per_audio_min(
    rtf: float | None,
    power_w: float,
    elec_price_kwh: float,
    hw_cost: float,
    amort_hours: float,
) -> float | None:
    """Per-audio-minute cost from measured RTF + operator cost inputs (₺/dk).

    Returns None unless the operator supplied a *complete* cost input — either a
    real electricity cost (power_w > 0 AND price > 0) or a real amortization cost
    (hw_cost > 0 AND amort_hours > 0). This decouples cost-enable from "price > 0"
    alone, so a hardware-only cost is not silently dropped, and a price without a
    power figure does not produce a misleading number (review #165, Codex MAJOR).
    """
    if not rtf or rtf <= 0:
        return None
    has_elec = power_w > 0 and elec_price_kwh > 0
    has_amort = hw_cost > 0 and amort_hours > 0
    if not (has_elec or has_amort):
        return None
    cph = local_cost_per_hour(
        power_w if has_elec else 0.0,
        elec_price_kwh if has_elec else 0.0,
        hw_cost if has_amort else 0.0,
        amort_hours if has_amort else 0.0,
    )["total"]
    return round(cost_per_audio_minute(cph, audio_minutes_per_wall_hour(rtf)), 5)


def _wav_duration(path: Path) -> float:
    try:
        with wave_mod.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else 0.0
    except (wave_mod.Error, EOFError, OSError):
        return 0.0


def _load_refs(samples_dir: Path, manifest: Path | None) -> dict[str, str]:
    refs: dict[str, str] = {}
    if manifest and manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for item in data.get("samples", []):
            wav_name = str(item.get("wav", ""))
            text = str(item.get("text") or item.get("sentence") or "")
            if wav_name and text:
                refs[wav_name] = text
    for txt in samples_dir.glob("*.txt"):
        refs[txt.stem + ".wav"] = txt.read_text(encoding="utf-8").strip()
    return refs


def _vram_mb() -> int:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    )
    return int(out.decode().splitlines()[0].strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="medium")
    ap.add_argument("--compute", default="float16")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--samples-dir", default="tests/fixtures/wer-cv17-tr")
    ap.add_argument("--manifest", default="tests/fixtures/wer-common-voice-tr/ground-truth.json")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--tag", default="", help="label for the JSON result row")
    # #161 cost/dakika — fully parametric, no baked-in assumptions. Cost is
    # reported only when the operator supplies a complete cost input (see
    # _cost_per_audio_min); otherwise null.
    ap.add_argument("--power-w", type=float, default=0.0, help="GPU power draw (W); 0 = unset")
    ap.add_argument("--elec-price-kwh", type=float, default=0.0, help="price/kWh; 0 = unset")
    ap.add_argument("--hw-cost", type=float, default=0.0, help="hardware cost for amortization")
    ap.add_argument("--amort-hours", type=float, default=0.0, help="amortization horizon (hours)")
    ap.add_argument("--currency", default="TRY", help="currency label for cost provenance")
    args = ap.parse_args()

    # Warn on partial cost inputs so a silently-dropped term isn't mistaken for
    # "no cost" (review #165): price without power, or hardware without horizon.
    if args.elec_price_kwh > 0 and args.power_w <= 0:
        print(
            "WARN: --elec-price-kwh set but --power-w missing → electricity cost skipped",
            file=sys.stderr,
        )
    if args.hw_cost > 0 and args.amort_hours <= 0:
        print(
            "WARN: --hw-cost set but --amort-hours missing → amortization skipped",
            file=sys.stderr,
        )

    samples_dir = Path(args.samples_dir)
    refs = _load_refs(samples_dir, Path(args.manifest) if args.manifest else None)
    wavs = sorted(p for p in samples_dir.glob("*.wav") if p.name in refs)
    if args.limit > 0:
        wavs = wavs[: args.limit]
    if not wavs:
        print(f"NO SAMPLES with references in {samples_dir}", file=sys.stderr)
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

    from faster_whisper import WhisperModel

    load_start = time.perf_counter()
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute)
    load_sec = time.perf_counter() - load_start

    pairs: list[tuple[str, str]] = []
    latencies: list[float] = []
    audio_sec_total = 0.0
    for wav in wavs:
        t0 = time.perf_counter()
        segments, _info = model.transcribe(str(wav), language="tr", beam_size=args.beam)
        hyp = " ".join(s.text.strip() for s in segments).strip()
        latencies.append((time.perf_counter() - t0) * 1000)
        audio_sec_total += _wav_duration(wav)
        pairs.append((refs[wav.name], hyp))
        print(f"  [{len(pairs)}/{len(wavs)}] {wav.name}", file=sys.stderr)

    stop.set()
    result = corpus_wer(pairs)
    lat_sorted = sorted(latencies)
    p95 = lat_sorted[min(len(lat_sorted) - 1, int(0.95 * len(lat_sorted)))]
    proc_sec = sum(latencies) / 1000.0
    rtf_val = proc_sec / audio_sec_total if audio_sec_total else None

    # #161 cost/dakika via the saf, test-edilebilir helper (review #165).
    cost_per_min = _cost_per_audio_min(
        rtf_val, args.power_w, args.elec_price_kwh, args.hw_cost, args.amort_hours
    )

    row = {
        "tag": args.tag or f"{args.model}-{args.compute}",
        "model": args.model,
        "compute": args.compute,
        "device": args.device,
        "beam": args.beam,
        "n_samples": len(pairs),
        "wer": round(float(result["corpus_wer"]), 4),  # type: ignore[arg-type]
        "substitutions": result["substitutions"],
        "deletions": result["deletions"],
        "insertions": result["insertions"],
        "ref_words": result["ref_words"],
        "p50_ms": round(statistics.median(latencies)),
        "p95_ms": round(p95),
        "audio_sec": round(audio_sec_total, 1),
        "rtf": round(rtf_val, 3) if rtf_val else None,
        "cost_per_audio_min": cost_per_min,
        # Cost provenance (review #165): a bare cost figure is meaningless without
        # the inputs + currency it was produced from.
        "cost_inputs": {
            "power_w": args.power_w,
            "elec_price_kwh": args.elec_price_kwh,
            "hw_cost": args.hw_cost,
            "amort_hours": args.amort_hours,
            "currency": args.currency,
        },
        "model_load_sec": round(load_sec, 1),
        "peak_vram_mb": peak["mb"],
    }
    print(json.dumps(row, ensure_ascii=False))
    _cost = f", cost/dk {cost_per_min} {args.currency}" if cost_per_min is not None else ""
    print(
        f"== {row['tag']}: WER {float(row['wer']):.2%} on {row['n_samples']} samples, "
        f"p50 {row['p50_ms']}ms, RTF {row['rtf']}, peak VRAM {row['peak_vram_mb']}MB{_cost}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
