"""#161 (T-B) diarization matrix runner — DER + VRAM + RTF per backend.

Pure-measurement tool (the diarization sibling of live-stt's wer_matrix.py).
Runs a diarization backend over an audio set, scores DER against RTTM
ground-truth, and reports one JSON row per run — feeding the ADR-0033 decision
with EVIDENCE (önce ölç, sonra karar: WER #35 → ADR-0031 disiplini).

Backends (ADR-0033 candidates), all behind ONE harness so the comparison is
apples-to-apples (issue #161: "pyannote vs alternatif"):
  - `pyannote`    : end-to-end pyannote.audio 3.1 pipeline (gated; needs token).
  - `speechbrain` : the ALTERNATIVE — energy-VAD segments + SpeechBrain ECAPA
                    speaker embeddings + agglomerative cosine clustering. Uses
                    the NON-gated `speechbrain/spkrec-ecapa-voxceleb` model
                    (no HF token). The VAD + clustering are pure-numpy and unit
                    tested on CPU; only the embedding step needs the GPU host.
  - `nemo`        : stub — raises a clear message until its adapter lands.

Ground-truth: RTTM (diarization standard). Reference resolution per audio:
  `<stem>.rttm` next to the wav.

DER scoring uses pyannote.metrics (Hungarian speaker mapping) — anonymous
SPEAKER_xx labels in hyp/ref are optimally matched, so label numbering differs
harmlessly. No voiceprint/embedding is stored (ADR-0030 boundary): embeddings
are computed in-memory for clustering only, never persisted or returned.

Usage (GPU host):
  # pyannote (gated → token):
  DIA_HF_TOKEN=hf_xxx python scripts/diar_matrix.py --backend pyannote \
      --model pyannote/speaker-diarization-3.1 --device cuda \
      --audio-dir tests/fixtures/diar-tr --tag pyannote-3.1
  # speechbrain alternative (no token):
  python scripts/diar_matrix.py --backend speechbrain --device cuda \
      --audio-dir tests/fixtures/diar-tr --tag speechbrain-ecapa
JSON row to stdout; human summary to stderr (sweeps redirect stdout to a file).
"""
# ruff: noqa: T201, S603, S607 - measurement CLI: prints are the output;
# nvidia-smi is a fixed-arg trusted binary (same pattern as wer_matrix.py).

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
import wave as wave_mod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

SB_DEFAULT_MODEL = "speechbrain/spkrec-ecapa-voxceleb"


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


@contextlib.contextmanager
def _trusted_torch_load() -> Iterator[None]:
    """Allow loading pre-2.6 checkpoints (pyannote/speechbrain weights are trusted).

    torch 2.6 flipped torch.load's default to weights_only=True; the official
    pyannote / speechbrain checkpoints predate that and are a trusted source, so
    we force weights_only=False for the duration of the model load only, then
    restore the original torch.load.
    """
    import torch  # type: ignore[import-not-found]

    orig = torch.load

    def _patched(*a: object, **k: object) -> object:
        k["weights_only"] = False
        return orig(*a, **k)

    torch.load = _patched  # type: ignore[assignment]
    try:
        yield
    finally:
        torch.load = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# pyannote backend (end-to-end pipeline)
# --------------------------------------------------------------------------- #
def run_pyannote(model: str, device: str, token: str) -> tuple[object, float]:
    """Load pyannote pipeline; return (pipeline, load_sec). Heavy import deferred."""
    import torch  # type: ignore[import-not-found]
    from pyannote.audio import Pipeline

    with _trusted_torch_load():
        t0 = time.perf_counter()
        pipeline = Pipeline.from_pretrained(model, use_auth_token=token)
    if device == "cuda":
        pipeline.to(torch.device("cuda"))
    return pipeline, time.perf_counter() - t0


def _pyannote_turns(pipeline: object, wav: Path) -> list[Turn]:
    annotation = pipeline(str(wav))  # type: ignore[operator]
    return [
        Turn(start=float(seg.start), end=float(seg.end), speaker=str(label))
        for seg, _, label in annotation.itertracks(yield_label=True)
    ]


# --------------------------------------------------------------------------- #
# speechbrain backend (ALTERNATIVE): energy-VAD + ECAPA embeddings + clustering
# Pure-numpy VAD and clustering live here so they are unit-testable on CPU
# without torch/speechbrain installed (the embedding step is the only GPU part).
# --------------------------------------------------------------------------- #
def _read_wav_mono16k(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV as mono float32 in [-1, 1], resampled to 16 kHz if needed."""
    import numpy as np

    with wave_mod.open(str(path), "rb") as w:
        rate = w.getframerate()
        n_ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    if rate != 16000 and len(data) > 1:
        idx = np.arange(0, len(data), rate / 16000.0)
        data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
        rate = 16000
    return data, rate


def energy_vad(
    samples: np.ndarray,
    rate: int,
    frame_ms: float = 30.0,
    hop_ms: float = 10.0,
    rel_threshold_db: float = 25.0,
    min_speech_ms: float = 300.0,
    min_gap_ms: float = 200.0,
) -> list[tuple[float, float]]:
    """Voice-activity segments via relative frame energy (pure numpy, no model).

    A frame is "speech" if its RMS is within `rel_threshold_db` dB of the loudest
    frame. Adjacent speech frames are merged; gaps shorter than `min_gap_ms` are
    bridged and segments shorter than `min_speech_ms` are dropped. Returns a list
    of (start_sec, end_sec). Deterministic → unit-testable.
    """
    import numpy as np

    if samples.size == 0:
        return []
    flen = max(1, int(rate * frame_ms / 1000.0))
    hop = max(1, int(rate * hop_ms / 1000.0))
    n_frames = 1 + max(0, (len(samples) - flen) // hop)
    if n_frames <= 0:
        return []
    rms = np.empty(n_frames, dtype=np.float64)
    for i in range(n_frames):
        frame = samples[i * hop : i * hop + flen]
        rms[i] = math.sqrt(float(np.mean(frame * frame))) if frame.size else 0.0
    ref = float(rms.max())
    if ref <= 0.0:
        return []
    db = 20.0 * np.log10(np.maximum(rms, 1e-10) / ref)
    speech = db >= -abs(rel_threshold_db)

    raw_segs: list[tuple[float, float]] = []
    seg_start: int | None = None
    for i in range(n_frames):
        if speech[i] and seg_start is None:
            seg_start = i
        elif not speech[i] and seg_start is not None:
            raw_segs.append((seg_start * hop / rate, (i * hop + flen) / rate))
            seg_start = None
    if seg_start is not None:
        raw_segs.append((seg_start * hop / rate, (n_frames * hop + flen) / rate))

    merged: list[list[float]] = []
    gap = min_gap_ms / 1000.0
    for s, e in raw_segs:
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    min_len = min_speech_ms / 1000.0
    total = len(samples) / rate
    return [(s, min(e, total)) for s, e in merged if (e - s) >= min_len]


def agglomerative_labels(
    embeddings: np.ndarray, num_speakers: int = 0, distance_threshold: float = 0.7
) -> list[int]:
    """Average-linkage agglomerative clustering on cosine distance (pure numpy).

    If `num_speakers` > 0 the merge stops at that many clusters; otherwise it
    stops when the closest pair exceeds `distance_threshold`. Labels are
    renumbered by first appearance so SPEAKER_00 is whoever speaks first
    (stable, deterministic anonymous labels).
    """
    import numpy as np

    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [0]
    x = np.asarray(embeddings, dtype=np.float64)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    x = x / np.maximum(norms, 1e-12)
    sim = x @ x.T
    dist = 1.0 - sim

    clusters: list[list[int]] = [[i] for i in range(n)]
    target = num_speakers if num_speakers > 0 else 1
    while len(clusters) > target:
        best = (float("inf"), -1, -1)
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                pairs = [dist[i, j] for i in clusters[a] for j in clusters[b]]
                d = float(sum(pairs) / len(pairs))
                if d < best[0]:
                    best = (d, a, b)
        if num_speakers <= 0 and best[0] > distance_threshold:
            break
        _, a, b = best
        clusters[a].extend(clusters[b])
        del clusters[b]

    label_of: dict[int, int] = {}
    for cid, members in enumerate(clusters):
        for idx in members:
            label_of[idx] = cid
    ordered = [label_of[i] for i in range(n)]
    remap: dict[int, int] = {}
    out: list[int] = []
    for lab in ordered:
        if lab not in remap:
            remap[lab] = len(remap)
        out.append(remap[lab])
    return out


def run_speechbrain(model: str, device: str) -> tuple[object, float]:
    """Load the SpeechBrain ECAPA encoder (non-gated); return (encoder, load_sec)."""
    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError:  # older speechbrain (<1.0) layout
        from speechbrain.pretrained import EncoderClassifier

    t0 = time.perf_counter()
    with _trusted_torch_load():
        encoder = EncoderClassifier.from_hparams(
            source=model,
            run_opts={"device": device},
            savedir=os.path.join(os.getenv("TEMP", "/tmp"), "sb-ecapa"),  # noqa: S108
        )
    return encoder, time.perf_counter() - t0


def _speechbrain_turns(
    encoder: object, wav: Path, num_speakers: int, distance_threshold: float
) -> list[Turn]:
    import numpy as np
    import torch  # type: ignore[import-not-found]

    samples, rate = _read_wav_mono16k(wav)
    segs = energy_vad(samples, rate)
    if not segs:
        return []
    embs: list[np.ndarray] = []
    for s, e in segs:
        chunk = samples[int(s * rate) : int(e * rate)]
        if chunk.size < rate // 10:  # <100 ms — too short to embed reliably
            chunk = np.pad(chunk, (0, rate // 10 - chunk.size))
        tensor = torch.tensor(chunk, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            emb = encoder.encode_batch(tensor)  # type: ignore[operator]
        embs.append(emb.squeeze().detach().cpu().numpy())
    labels = agglomerative_labels(np.asarray(embs), num_speakers, distance_threshold)
    return [
        Turn(start=round(s, 3), end=round(e, 3), speaker=f"SPEAKER_{lab:02d}")
        for (s, e), lab in zip(segs, labels, strict=True)
    ]


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="pyannote", choices=["pyannote", "speechbrain", "nemo"])
    ap.add_argument("--model", default="", help="model id (backend default used if empty)")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--audio-dir", default="tests/fixtures/diar-tr")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--tag", default="", help="label for the JSON result row")
    ap.add_argument("--num-speakers", type=int, default=0, help="0 = auto (speechbrain clustering)")
    ap.add_argument("--cluster-threshold", type=float, default=0.7, help="speechbrain cosine cut")
    ap.add_argument("--evidence", default="", help="if set, append the JSON row to this file")
    args = ap.parse_args()

    if args.backend == "nemo":
        print(
            "backend 'nemo' adapter not wired yet — ADR-0033 candidate; "
            "pyannote + speechbrain ölçülüp baseline kurulduktan sonra eklenecek.",
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

    if args.backend == "pyannote":
        model_used = args.model or "pyannote/speaker-diarization-3.1"
        token = os.getenv("DIA_HF_TOKEN", "")
        if not token:
            print("DIA_HF_TOKEN not set — gated pyannote model needs it", file=sys.stderr)
            sys.exit(4)
        pipeline, load_sec = run_pyannote(model_used, args.device, token)

        def diarize_fn(wav: Path) -> list[Turn]:
            return _pyannote_turns(pipeline, wav)
    else:  # speechbrain
        model_used = args.model or SB_DEFAULT_MODEL
        encoder, load_sec = run_speechbrain(model_used, args.device)

        def diarize_fn(wav: Path) -> list[Turn]:
            return _speechbrain_turns(encoder, wav, args.num_speakers, args.cluster_threshold)

    ders: list[float] = []
    latencies: list[float] = []
    audio_sec_total = 0.0
    for wav in wavs:
        ref = load_rttm(wav.with_suffix(".rttm"))
        t0 = time.perf_counter()
        hyp = diarize_fn(wav)
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
        "model": model_used,
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
    line = json.dumps(row, ensure_ascii=False)
    print(line)
    print(
        f"== {row['tag']}: DER {row['der']:.2%} on {row['n_samples']} samples, "
        f"p50 {row['p50_ms']}ms, RTF {row['rtf']}, peak VRAM {row['peak_vram_mb']}MB",
        file=sys.stderr,
    )
    if args.evidence:
        ev = Path(args.evidence)
        ev.parent.mkdir(parents=True, exist_ok=True)
        with ev.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"== appended evidence row → {ev}", file=sys.stderr)


if __name__ == "__main__":
    main()
