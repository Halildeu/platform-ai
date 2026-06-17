# #161 (T-B) Diarization DER + VRAM + RTF Matris Raporu

**Donanım:** RTX 4070 (8 GB) · **Araç:** `services/diarization-service/scripts/diar_matrix.py`
**Fixture:** sentetik çok-konuşmacı (CV-TR klipleri, `make_synthetic_diar.py`; CI-only)
**Ham satırlar:** `docs/evidence/diar-results-<tarih>.jsonl` (her koşu bir JSON satırı)
**Skor:** DER = pyannote.metrics (Hungarian eşleme; anonim SPEAKER_xx → optimal map)

> **Durum:** kısmi. pyannote tek-örnek (n=1) **smoke** ölçümü alındı; sweep (n>1)
> ve speechbrain alternatifi **GPU'da koşulmayı bekliyor** (komutlar §Çalıştırma).
> Bu rapor doldukça ADR-0033'ün karar tablosunu besler.

## Matris

`PENDING` = aynı fixture + RTX 4070 protokolünde henüz ölçülmedi.

| Tag | Backend | n | DER | DER p95 | p50 ms | RTF | model_load s | Peak VRAM |
|---|---|---|---|---|---|---|---|---|
| pyannote-3.1 (smoke) | pyannote | 1 | 45.04% | 45.04% | 866 | 0.036 | 6.7 | 2129 MB |
| **pyannote-3.1 (sweep)** | pyannote | **6** | **50.14%** | 55.27% | 1290 | 0.024 | 2.9 | 2155 MB |
| speechbrain-ecapa | speechbrain | 6 | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

Per-fixture DER (n=6, 2-speaker TR synthetic, varied turns/gap): 46.3 / 54.9 /
46.1 / 45.1 / 55.3 / 53.2 → mean 50.14%, p95 55.27%. Same fixtures will be
reused for the speechbrain row (apples-to-apples).

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
