# Performance Line #43 Execution Report

Issue: `#43 [Performance ölçüm] post-GPU WER + latency + memory + cost matrix güncelle`

Canonical issue body (read 2026-06-08 via `gh issue view 43`):

> GPU sonrası matrisi güncelle:
> - WER (GPU latency yüksek model kullanabilir)
> - Latency / audio minute
> - Peak VRAM
> - Toplam 4535/saat (cloud GPU veya elektrik+amortisman)
> - Production karar

Measurement-first. The harness records numbers; the production model choice is
read **off** the resulting matrix — no winner is hard-coded.

## Status

| Phase | Description | State |
|---|---|---|
| 0 | Methodology: WER metric, references, models, cost model | DONE |
| 1 | WER + cost modules + perf-matrix harness + unit tests | DONE (36 tests) |
| 2 | Real RTX 4070 matrix run (medium vs large-v3) | PENDING (GPU) |
| 3 | Production model recommendation from the matrix | PENDING |

---

## Phase 0 — Methodology

### Metrics

- **WER** (word error rate) = `(S + D + I) / N_ref` via word-level Levenshtein
  alignment, after Turkish-aware normalization (dotless/dotted-i mapping,
  lowercasing, punctuation strip). Implemented in `scripts/wer.py`.
- **Latency / audio-minute** = `total_processing_ms / total_audio_minutes`.
- **Real-time factor (RTF)** = `processing_time / audio_time` (RTF < 1 = faster
  than real time).
- **Peak VRAM** = max `nvidia-smi memory.used` sampled during the run.
- **Cost** = parametric (`scripts/cost.py`): local GPU
  `electricity + amortization` per hour vs a cloud GPU hourly rate, converted to
  **per-audio-minute** using measured throughput. Currency-agnostic.

> The issue's "Toplam 4535/saat" figure is **not** interpreted as a fixed
> constant — its unit/source is unclear. The cost model takes electricity price,
> GPU power, hardware cost, amortization horizon and a cloud hourly rate as
> inputs; the operator supplies the real numbers at decision time.

### Models compared

`medium` (current live/draft) vs `large-v3` (high-accuracy final). Both already
proven on the RTX 4070 (#41). The matrix shows the accuracy/latency/VRAM/cost
trade-off so the final-stt model (#39) decision is data-driven.

### Reference data (WER ground truth)

`scripts/download-cv17-tr-samples.py` writes `*.wav` + `*.txt` (reference
sentence) pairs from Mozilla Common Voice 17 TR (CC0). Two fixtures are
committed (`sample-tr-cv17-001/002`). A fuller WER PoC fetches 100-200 random
samples with `--selection random --manifest-json`. The harness scores every wav
that has a sibling `.txt`.

---

## Phase 1 — Harness (artifacts)

| File | Purpose |
|---|---|
| `scripts/wer.py` | Pure WER (normalize_tr, word_error_rate, corpus_wer) |
| `scripts/cost.py` | Pure parametric cost model (local vs cloud per audio-min) |
| `scripts/perf_client.py` | Stdlib client: transcribe fixtures, compute WER + latency |
| `scripts/gpu-perf-matrix.ps1` | Per-model container run, VRAM sampling, matrix table |
| `tests/unit/test_wer.py` | 10 WER unit tests |
| `tests/unit/test_cost.py` | 8 cost unit tests |

All pure logic is GPU-free and unit-tested; the GPU run only feeds audio in.

---

## Phase 2 — RTX 4070 Matrix Results

_To be appended after running `gpu-perf-matrix.ps1` on the GPU PC._

## Phase 3 — Production Decision

_To be appended: data-driven recommendation (accuracy vs latency vs VRAM vs cost)._

## Scope Boundaries

- No production model lock here — that is the decision **output** of the matrix
  (feeds #39 final-stt).
- No GitOps/production deploy.
- Cost figures are operator-supplied parameters, never baked-in constants.
- WER corpus size is limited by the committed fixtures unless more CV17 TR
  samples are downloaded first.
