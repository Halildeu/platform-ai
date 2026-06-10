# #40 Donanım Kararı — FİNAL: Lokal RTX 4070

**Tarih:** 2026-06-10
**Statü:** FINAL (ölçüm kanıtlı). Provisional rapor: `issue-40-hardware-decision-provisional-report.md`
**Karar:** Faz 24 STT compute = **mevcut lokal RTX 4070 host**. Cloud GPU yedek/patlama kapasitesi olarak değerlendirilir; k3d-prod node-pool'a GPU eklenmez (bu fazda).

## Kanıt 1 — Kapasite yeterli (ölçüldü, RTX 4070)

| Ölçüm | Değer | Kaynak |
|---|---|---|
| Shared-model multi-stream K=2 peak VRAM | 2475 MB | #42, `gpu-42-shared-stream-measurement.md` |
| K=2 throughput | 3.0 req/s | #42 |
| large-v3-turbo final-stt: WER / p50 / RTF / VRAM | 18.3% / 307 ms / 0.069 / 2548 MB | #35 matrisi |
| medium-int8 live draft: WER / p50 / RTF / VRAM | 20.3% / 349 ms / 0.079 / 2004 MB | #35 matrisi |
| Draft (medium-int8) + final (turbo) birlikte VRAM | ~4.6 GB ≪ 12 GB | toplama |

RTF ≈ 0.07 → tek kart **saatte ~1.700 ses-dakikası** işler (K=2). Hedef pilot yükü
(eşzamanlı birkaç toplantı) için bol kapasite; K=2 sweet-spot, K≥4 compute-bound (#42).

## Kanıt 2 — Maliyet (cost.py, parametreler operatörce güncellenebilir)

Varsayımlar: 200 W çekiş, ₺3,5/kWh, donanım ₺25.000 / 3 yıl amortisman, cloud GPU ₺25/saat.

| | ₺/saat | ₺/ses-dakikası |
|---|---|---|
| **Lokal RTX 4070** | **1,65** (0,70 elektrik + 0,95 amorti) | **0,00095** |
| Cloud GPU | 25,00 | 0,01438 |

→ Lokal **~15× ucuz**. Cloud ancak yük lokal kapasiteyi aşarsa (burst) anlamlı.

## Kanıt 3 — KVKK

Ses verisi **şirket içinde** kalır: yurt dışı aktarım (KVKK md. 9) tetiklenmez,
VERBIS/aydınlatma yükü sadeleşir. Cloud GPU seçeneğinde veri lokasyonu ve
işleyici sözleşmeleri ek hukuki iş üretir (#52/#53 kapsamı büyür).

## Riskler ve karşılıklar
- **R-HW-1 (donanım atıl kalma, #61):** Kart zaten mevcut (sunk cost); atıl kalma
  riski yeni satın almaya kıyasla yok denecek kadar az. Amortisman satırı bilinçli
  olarak maliyete eklendi — atıl kalsa bile kayıp ₺0,95/saat tavanlıdır.
- **Tek nokta arızası:** Kart arızasında geçici cloud GPU failover (Dockerfile.gpu
  taşınabilir, #41); RTO operasyon runbook'una eklenecek (#56 cutover planı).
- **Ölçek aşımı:** Saatte 1.700 ses-dk aşılırsa ikinci kart veya cloud burst —
  maliyet modeli `scripts/cost.py` ile yeniden hesaplanır.

## Sonuç
RTX 4070: kapasite ✅, maliyet ✅ (15×), KVKK ✅. Karar FINAL; müdür/maintainer
itirazında bu doküman üzerinden revize edilir.
