# #43 Post-GPU Performans + Maliyet Matrisi — FİNAL

**Tarih:** 2026-06-10
**Önceki parçalar:** `pr-perf-line-43-execution-report.md` (araçlar),
`services/live-stt-service/docs/pr-perf-line-43-shared-stream-update.md` (VRAM/latency),
`services/live-stt-service/docs/gpu-42-shared-stream-measurement.md` (#42 ham ölçüm).
Bu doküman eksik iki satırı doldurur: **WER** ve **₺/saat** → issue kapanış kriteri tamam.

## 1. WER (yeni — #35 matrisi, RTX 4070, 150 Common Voice TR örneği)

| Model | Temiz WER | Sentetik-bozuk WER | p50 | RTF | Peak VRAM |
|---|---|---|---|---|---|
| medium-fp16 | 20.63% | — | 355 ms | 0.078 | 2388 MB |
| medium-int8 | 20.32% | 30.94% | 349 ms | 0.079 | 2004 MB |
| large-v3-fp16 | **18.10%** | — | 506 ms | 0.110 | 4340 MB |
| **large-v3-turbo-fp16** | 18.30% | **25.58%** | **307 ms** | **0.069** | 2548 MB |

Kilit bulgu: bozuk koşulda (SNR 10 dB + 1.1× hız) turbo'nun üstünlüğü 2 → **5.4 puana** açılır.
Gerçek toplantı sesi bozuk koşula yakındır → **final-stt = large-v3-turbo**, live draft = medium-int8.

## 2. Latency / ses-dakikası
RTF 0.069 (turbo) → 1 ses-dakikası ≈ **4,1 sn** işlem. K=2 eşzamanlılıkta kart
saatte ≈ **1.739 ses-dakikası** işler (`cost.py: audio_minutes_per_wall_hour`).

## 3. Peak VRAM (özet, #42)
Shared-model multi-stream: K=1→4 için 2275→3003 MB (~düz). Eski process-pool K=4: ~7819 MB → ÇÖKTÜ.
Draft+final iki model birlikte: ~4.6 GB ≪ 12 GB.

## 4. ₺/saat (cost.py; parametreler operatörce güncellenebilir)
200 W, ₺3,5/kWh, ₺25.000 donanım / 3 yıl, cloud ₺25/saat varsayımlarıyla:

| | ₺/saat | ₺/ses-dk |
|---|---|---|
| Lokal RTX 4070 | **1,65** | **0,00095** |
| Cloud GPU | 25,00 | 0,01438 (≈15×) |

## 5. Production kararı
- **Mimari:** live draft = medium-int8 (shared backend K=2, #42) → final pass = large-v3-turbo.
- **Donanım:** lokal RTX 4070 (FINAL, `issue-40-hardware-decision-final.md`).
- **Eşzamanlılık:** K=2 (throughput tepe noktası; K≥4 compute-bound).
- **Kalan doğrulama:** gerçek toplantı pilotu (consent sonrası, #34 protokolü) — ADR-0031
  pilot teyidi; triangulation 2/3 ayak tamamlandı (#36 raporu).
