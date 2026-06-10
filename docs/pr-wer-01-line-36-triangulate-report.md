# #36 Triangulate Analiz Raporu (3 dataset)

**Tarih:** 2026-06-10 · **Yöntem:** Aynı model adaylarını üç bağımsız veri
koşulunda karşılaştır; karar yalnız tek datasete dayanmasın.

## Ayak 1 — Common Voice TR (temiz, okunmuş konuşma) ✅
150 örnek, CC0, ground-truth manifest (#33). Sonuç (#35 matrisi):
large-v3 (18.10%) ≈ **turbo (18.30%)** < medium (20.3-20.6%).

## Ayak 2 — Sentetik-bozuk koşul ✅
Aynı 150 referans; SNR 10 dB gürültü + 1.1× hız (`make_synthetic_tr.py` —
deterministik, seed'li). Toplantı odası bozulmasının kontrollü vekili.
Sonuç: **turbo 25.58% vs medium-int8 30.94%** → fark temizdeki 2 puandan
**5.4 puana** açılıyor. Büyük modelin gürbüzlük avantajı bozulmayla birleşik.

## Ayak 3 — Gerçek toplantı pilotu ⏳ (DEFERRED — operatör aksiyonu)
Protokol hazır (#34: consent şablonları, şifreli saklama, manuel ground truth).
Bekleyen: operatör onayı + katılımcı consent'i (KVKK). Bu rapor pilot WER'i
gelince Ek-A olarak güncellenir. **Karar pilotu beklemiyor** çünkü iki bağımsız
ayak aynı sıralamayı veriyor ve pilot yalnız mutlak değerleri kalibre edecek.

## Üçgenleme sonucu

| Kriter | medium-int8 | large-v3-turbo | Kazanan |
|---|---|---|---|
| Temiz WER | 20.32% | 18.30% | turbo |
| Bozuk WER | 30.94% | 25.58% | turbo (büyüyen farkla) |
| p50 latency | 349 ms | 307 ms | turbo |
| RTF (maliyet sürücüsü) | 0.079 | 0.069 | turbo |
| Peak VRAM | 2004 MB | 2548 MB | medium |
| İki model birlikte VRAM | ~4.6 GB ≪ 12 GB | uyumlu | — |

**Sonuç:** Final-stt = **large-v3-turbo-fp16**; live draft = **medium-int8**
(düşük VRAM + yeterli draft kalitesi; final pass hataları zaten revize ediyor,
#39 state machine). ADR-0031'e kanıt bölümü eklendi; statü pilot teyidine kadar
"evidence-backed provisional".

## Tehditler / sınırlar (dürüstlük bölümü)
- CV okuma konuşması; spontane toplantı dili (kesmeler, jargon) ölçülmedi → pilot.
- Sentetik bozulma beyaz gürültü; gerçek oda akustiği (yankı, uzak mikrofon) farklı.
- Tek GPU, tek koşu süiti; varyans tek modelde (medium-int8-synth ×2, aynı sonuç) doğrulandı.
