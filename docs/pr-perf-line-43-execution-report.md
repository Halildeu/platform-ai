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
| 2 | Real RTX 4070 matrix run (medium vs large-v3) | DONE (latency/VRAM solid; WER corpus too small) |
| 3 | Production model recommendation from the matrix | BLOCKED (needs bigger WER corpus + cost inputs) |

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

## Phase 2 — RTX 4070 Matrix Results (2026-06-08)

`gpu-perf-matrix.ps1 -Models medium,large-v3 -ComputeType float16` on RTX 4070
(8188 MiB), over the 2 committed CV17 TR fixtures (13 reference words total).

| Model | Ok | Errors | Corpus WER | Ref words | Latency ms / audio-min | RTF | Peak VRAM MiB |
|---|---|---|---|---|---|---|---|
| medium | 2 | 0 | 0.1538 | 13 | 3924.7 | 0.0654 | 2097 |
| large-v3 | 2 | 0 | 0.1538 | 13 | 5854.8 | 0.0976 | 3921 |

### Findings (latency / VRAM are decision-grade)

- **Latency:** large-v3 is **~49 % slower** than medium per audio-minute
  (5855 vs 3925 ms/min).
- **VRAM:** large-v3 uses **~87 % more** VRAM (3921 vs 2097 MiB).
- **Both are far faster than real time** on GPU: RTF 0.065 (medium, ~15×) and
  0.098 (large-v3, ~10×). Neither is latency-bound on the RTX 4070.

### WER is NOT decision-grade yet (honest limitation)

Both models scored **0.1538** (2 word errors out of **13** reference words). With
only 13 words this is **noise, not signal** — it does **not** mean large-v3 and
medium are equally accurate. A real accuracy decision requires a proper corpus
(100-200 CV17 TR samples via `download-cv17-tr-samples.py --selection random`).
The harness pipeline is proven end-to-end; only the corpus size is the blocker.

## Phase 3 — Production Decision

**BLOCKED on two inputs:**

1. **Real WER corpus** — rerun the matrix after downloading 100-200 CV17 TR
   samples so the accuracy column becomes meaningful.
2. **Cost inputs** — electricity price (per kWh), GPU power draw, hardware cost,
   amortization horizon, and a cloud GPU hourly rate (and clarification of the
   issue's "4535/saat" figure). The `cost.py` model turns these into a per-audio
   -minute comparison once supplied.

Preliminary, latency/VRAM-only (NOT the final accuracy call): both models run
comfortably real-time on the RTX 4070, so the choice is an **accuracy-vs-VRAM**
trade-off — large-v3 only justifies its ~1.9× VRAM and ~1.5× latency if the
larger-corpus WER shows a real accuracy gain. To be decided in a follow-up run.

## Scope Boundaries

- No production model lock here — that is the decision **output** of the matrix
  (feeds #39 final-stt).
- No GitOps/production deploy.
- Cost figures are operator-supplied parameters, never baked-in constants.
- WER corpus size is limited by the committed fixtures unless more CV17 TR
  samples are downloaded first.
