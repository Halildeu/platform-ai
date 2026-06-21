# #161 (T-B) Diarization DER + VRAM + RTF Matris Raporu

**Donanım:** RTX 4070 (8 GB) · **Araç:** `services/diarization-service/scripts/diar_matrix.py`
**Fixture:** sentetik çok-konuşmacı (CV-TR klipleri, `make_synthetic_diar.py`; CI-only)
**Ham satırlar:** `docs/evidence/diar-results-<tarih>.jsonl` (her koşu bir JSON satırı)
**Skor:** DER = pyannote.metrics (Hungarian eşleme; anonim SPEAKER_xx → optimal map)

> **Durum:** pyannote + speechbrain **GPU'da ölçüldü** (aynı 6 fixture,
> apples-to-apples). Tek kalan: gerçek toplantı (pilot) DER — go-live #59'a bağlı.

## Matris (RTX 4070, aynı 6 sentetik fixture — apples-to-apples)

| Tag | Backend | n | DER (ort.) | DER max | p50 ms | RTF | model_load s | GPU mem (toplam tepe) |
|---|---|---|---|---|---|---|---|---|
| pyannote-3.1 (smoke) | pyannote | 1 | 45.04% | 45.04% | 866 | 0.036 | 6.7 | 2129 MB |
| **pyannote-3.1** | pyannote | 6 | **50.14%** | 55.27% | 1290 | 0.024 | 2.9 | 2155 MB |
| **speechbrain-ecapa** | speechbrain | 6 | 56.64% | 57.62% | **144** | **0.006** | 3.4 | **307 MB** |

Per-fixture DER (n=6, 2-konuşmacı TR sentetik, değişken tur/boşluk):
- pyannote: 46.3 / 54.9 / 46.1 / 45.1 / 55.3 / 53.2 → ort. 50.14%
- speechbrain: 57.4 / 57.6 / 57.1 / 55.2 / 57.1 / 55.2 → ort. 56.64%

> **Schema notu (#189 review).** Bu evidence/rapor pre-#189 koşusudur, current
> `diar_matrix.py` schema'sına şu farklarla bakın: (1) Buradaki DER **per-file
> ortalama** (`der`); araç artık ek olarak **duration-weighted `der_corpus`**
> üretir (asıl karar metriği) — bir sonraki sweep'te dolar. (2) "DER max" sütunu
> n=6'da p95 indexinin denk geldiği maksimumdur, gerçek persentil değil. (3)
> **GPU mem = toplam GPU0 kullanımı**, backend-izole delta DEĞİL (izole
> `.venv-diar`'da ölçüldü → tek süreç); araç artık `peak_vram_delta_mb`
> (peak − pre-load baseline) de raporlar. "7× az" izole-venv toplamına dayanır;
> backend-delta sonraki sweep'te netleşir.

## pyannote vs alternatif (kıyas)

| Eksen | Kazanan | Fark |
|---|---|---|
| **Doğruluk (DER)** | **pyannote** | ~6.5 puan daha düşük DER (50.14% vs 56.64%) |
| **VRAM** | **speechbrain** | **7× az** (307 MB vs 2155 MB) — 8 GB sınırında kritik |
| **Hız (p50/RTF)** | **speechbrain** | **~9× hızlı** (144ms vs 1290ms; RTF 0.006 vs 0.024) |
| Model yükleme | ~eşit | 3.4s vs 2.9s |

Okuma: pyannote bu sentetik sette daha doğru; speechbrain çok daha hafif/hızlı.
8 GB bütçesinde speechbrain'in 307 MB'ı cazip — ama doğruluk farkı gerçek
veriyle (pilot) teyit edilmeli. Mutlak DER'ler yüksek (sentetik yapaylık);
**göreli sıralama** karar için anlamlı, mutlak değer pilotla kalibre edilecek
(ADR-0031 WER disiplininin aynısı).

## Bulgular (şu ana kadar)

1. **Araç uçtan uca çalışıyor:** model indi → GPU'ya yüklendi → Türkçe ses
   diarize edildi → DER hesaplandı → JSON kanıt üretildi. pyannote yolu kanıtlı.
2. **VRAM kritik bulgu:** pyannote tek başına ~2.1 GB. STT (~4.6) + pyannote
   (2.1) = ~6.7 GB < 8 GB sığar; ama Ollama (~5) eklenince taşar → **diarization
   canlı değil, post-processing** (ADR-0033, PR #163 ile tutarlı).
3. **RTF 0.036:** 24 sn ses <1 sn'de işlendi — GPU'da hız sorun değil.
4. **DER %45 karar sayısı DEĞİL:** sentetik fixture yapay (örtüşme yok, tek
   örnek, ardışık turlar → pyannote konuşmacı sayısını şaşırabilir). Anlamlı
   değerlendirme için sweep (n>1) + alternatif + pilot gerekir.

## Karar girdisi

- Backend kararı **veriyle** alınacak (ADR-0033 Promote-to-ACCEPTED tetikleyicileri).
- Mutlak DER ve diarization hedefi **pilot** ayağına bağlı (ADR-0031'in pilot
  disipliniyle aynı); go-live #59 / consent'e bağımlı.

## Çalıştırma (GPU host — `.venv-diar` içinde)

```powershell
cd services\diarization-service
# pyannote sweep (n>1) — token aynı oturumda:
.\scripts\diar_sweep.ps1 -Backend pyannote -Tag pyannote-3.1
# alternatif (token gerekmez; speechbrain kurulu olmali):
pip install -r requirements.txt -r requirements-speechbrain.txt
.\scripts\diar_sweep.ps1 -Backend speechbrain -Tag speechbrain-ecapa
```

Her koşu sonucu `docs/evidence/diar-results-<tarih>.jsonl`'e eklenir; satırlar
bu rapordaki PENDING hücrelerine işlenir.
