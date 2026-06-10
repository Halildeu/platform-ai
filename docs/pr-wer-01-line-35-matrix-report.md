# #35 WER + 8-Metrik Matris Raporu

**Tarih:** 2026-06-10 · **Donanım:** RTX 4070 · **Veri:** Common Voice 17.0 TR test, 150 örnek (CC0, PII'siz manifest #33)
**Araç:** `scripts/wer_matrix.py` (corpus WER = mikro-ortalama, TR-doğru normalizasyon `scripts/wer.py`)
**Ham satırlar:** `wer-results.jsonl` (GPU host; kanıt dosyası repoya eklenecek)

## Matris — temiz set (150 örnek, beam 5, language=tr)

| Metrik | medium-fp16 | medium-int8 | large-v3-fp16 | **large-v3-turbo-fp16** |
|---|---|---|---|---|
| 1. WER | 20.63% | 20.32% | **18.10%** | 18.30% |
| 2. p50 latency | 355 ms | 349 ms | 506 ms | **307 ms** |
| 3. RTF | 0.078 | 0.079 | 0.110 | **0.069** |
| 4. Peak VRAM | 2388 MB | **2004 MB** | 4340 MB | 2548 MB |
| 5-8. S/D/I + p95 | `wer-results.jsonl` satırlarında (per-run JSON) | | | |

## Sentetik-bozuk set (aynı 150 referans; SNR 10 dB beyaz gürültü + 1.1× hız, `make_synthetic_tr.py`)

| Metrik | medium-int8 | large-v3-turbo-fp16 |
|---|---|---|
| WER | 30.94% | **25.58%** |
| Bozulma (temize göre) | +10.6 puan | **+7.3 puan** |
| p50 / RTF / VRAM | 346 ms / 0.093 / 2004 MB | 308 ms / 0.075 / 2548 MB |

Determinizm: medium-int8-synth iki bağımsız koşuda aynı WER (%30.94) üretti.

## Bulgular
1. **large-v3-turbo = tatlı nokta.** large-v3 doğruluğunun 0.2 puan dahilinde, ama
   medium'dan bile hızlı (307<349 ms) ve large-v3'ün yarı VRAM'i.
2. **Bozuk koşulda fark açılıyor** (2 → 5.4 puan): gerçek toplantı sesine en yakın
   senaryoda turbo belirgin üstün. Production final modeli için belirleyici kanıt.
3. **medium-int8 ≈ medium-fp16 doğrulukta** (20.32 vs 20.63 — gürültü payı içinde),
   ama en düşük VRAM → canlı draft rolü için doğru seçim (ADR-0031 ile uyumlu).
4. CV okuma konuşması olduğundan mutlak WER'ler iyimser; **göreli sıralama** karar
   için yeterli, mutlak değerler pilotla (3. ayak) teyit edilecek.

## Karar girdisi
- Live draft: **medium-int8** · Final pass: **large-v3-turbo-fp16** (ADR-0031 güncellenecek)
- #36 triangulate raporu bu veriyi 2 ayak olarak kullanır; #43 matrisi tamamlandı.
