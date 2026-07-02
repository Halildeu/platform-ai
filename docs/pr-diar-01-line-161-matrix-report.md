# #161 (T-B) Diarization DER + VRAM + RTF Matris Raporu

**Donanım:** RTX 4070 (8 GB) · **Araç:** `services/diarization-service/scripts/diar_matrix.py`
**Fixture:** sentetik çok-konuşmacı (CV-TR klipleri, `make_synthetic_diar.py`) — **synthetic-smoke**
**Ham satırlar:** `docs/evidence/diar-results-2026-06-17.jsonl` (her koşu bir JSON satırı)
**Skor:** DER = pyannote.metrics (Hungarian eşleme; anonim SPEAKER_xx → optimal map),
**collar = 0.25 s** (dscore standardı), skip_overlap = false

> **Durum:** pyannote + speechbrain **GPU'da ölçüldü** (aynı 6 fixture, collar=0.25,
> apples-to-apples). Bu bir **synthetic-smoke** ölçümüdür — baseline DER DEĞİL,
> backend sıralaması için kullanılamaz (ADR-0033). Tek kalan: gerçek toplantı
> (pilot) DER — go-live #59'a bağlı.

## Matris (RTX 4070, aynı 6 sentetik fixture, collar=0.25 — apples-to-apples)

| Tag | Backend | n | DER | DER (max) | p50 ms | RTF | model_load s | GPU0 toplam tepe² |
|---|---|---|---|---|---|---|---|---|
| **pyannote-collar025** | pyannote | 6 | **47.8%** | 53.2% | 1293 | 0.024 | 7.3 | 2155 MB |
| **speechbrain-collar025** | speechbrain | 6 | 54.6% | 55.6% | **140** | **0.003** | 3.3 | **307 MB** |

Per-fixture DER (n=6, 2-konuşmacı TR sentetik, collar=0.25):
- pyannote: 43.8 / 52.8 / 43.6 / 42.5 / 53.2 / 51.0 → ort. 47.8%
- speechbrain: 55.4 / 55.6 / 55.1 / 53.1 / 55.1 / 53.1 → ort. 54.6%

> n=6'da "p95" indeksi son elemana (max) düştüğü için kolon **DER (max)** olarak
> dürüstçe etiketlendi (gerçek bir yüzdelik değil). collar=0 ile ölçüm
> pyannote 50.14 / speechbrain 56.64 vermişti; collar=0.25 her ikisini ~2pt
> düşürdü ama **sıralamayı değiştirmedi** — yine de bu bir sıralama *kanıtı* değil.

> ² **VRAM = toplam GPU0 `memory.used` tepe, backend-izole DEĞİL (#189).** İzole
> `.venv-diar`'da (tek süreç) ölçüldü; "7× az" göreli okuma orada geçerli ama mutlak
> MB GPU0'daki her şeyi içerir. Güncel `diar_matrix.py` ayrıca `peak_vram_delta_mb`
> (tepe − pre-load baseline) ve duration-weighted `der_corpus` (asıl karar metriği)
> üretir; ikisi de sonraki sweep'te evidence'a düşer.

## pyannote vs alternatif (kıyas — sıralama iddiası YOK)

| Eksen | Gözlem (synthetic-smoke) |
|---|---|
| **Doğruluk (DER)** | pyannote bu sette daha düşük DER (47.8% vs 54.6%) — **ama backend UNDECIDED**; sentetik smoke sıralama kanıtı değil (örtüşme yok, klipler birebir tekrar → speaker confusion ölçülmüyor) |
| **VRAM** | speechbrain **7× az** (307 MB vs 2155 MB) — 8 GB sınırında kritik |
| **Hız (p50/RTF)** | speechbrain **~9× hızlı** (140ms vs 1293ms; RTF 0.003 vs 0.024) |
| Model yükleme | ~eşit (3.3s vs 7.3s; ilk indirme/cache etkisi) |

Okuma: bu **synthetic-smoke** sette pyannote daha düşük DER, speechbrain çok daha
hafif/hızlı. **Hiçbir backend seçilmedi** (ADR-0033: UNDECIDED). Mutlak DER ve
göreli sıralama, gerçekçi fixture (örtüşme + farklı/birebir-olmayan ses) +
pilotla belirlenecek — sentetik-smoke yalnızca harness'in çalıştığını kanıtlar.

## Bulgular (şu ana kadar)

1. **Araç uçtan uca çalışıyor:** model indi → GPU'ya yüklendi → Türkçe ses
   diarize edildi → DER (collar=0.25) hesaplandı → JSON kanıt üretildi.
2. **VRAM kritik bulgu:** pyannote tek başına ~2.1 GB. STT (~4.6) + pyannote
   (2.1) = ~6.7 GB < 8 GB sığar; ama Ollama (~5) eklenince taşar → **diarization
   canlı değil, post-processing** (ADR-0033 ile tutarlı).
3. **RTF düşük:** ses gerçek-zamandan çok hızlı işlendi — GPU'da hız sorun değil.
4. **Bu DER'ler karar sayısı DEĞİL:** synthetic-smoke fixture yapay (örtüşme yok,
   klipler birebir tekrar → konuşmacı karışması ölçülmüyor). Backend kararı için
   gerçekçi fixture + pilot gerekir.

## Reproducibility & CI notları

- **Model pin:** `diar_matrix.py --revision <commit>` HF model sürümünü pinler.
  Bu koşuda revision pinlenmedi (`revision: null`); **promote-grade koşuda commit
  pinlenmeli** ki sonuçlar yeniden üretilebilsin.
- **CI:** `pyannote.metrics` artık `requirements-dev.txt`'te (#189) → **DER scorer
  unit testleri (`compute_der`, identical/miss/exact-overlap) CI'da KOŞAR**,
  VAD/clustering/RTTM ile birlikte. Yeşil CI artık DER-scorer mantığını kanıtlar.
  Ama gerçek **model/GPU backend ölçümü** (pyannote/speechbrain üzerinde DER) hâlâ
  host/pilot işidir — promote-grade DER GPU host'ta üretilir.

## Karar girdisi

- Backend kararı **veriyle** alınacak (ADR-0033 Promote-to-ACCEPTED tetikleyicileri:
  collar=0.25 ✅, gerçekçi fixture ⬜, pilot ⬜).
- Mutlak DER ve diarization hedefi **pilot** ayağına bağlı; go-live #59 / consent'e bağımlı.

## Çalıştırma (GPU host — `.venv-diar` içinde)

```powershell
cd services\diarization-service
# pyannote (collar=0.25 default; promote koşusunda --revision <commit> ekle):
.\scripts\diar_sweep.ps1 -Backend pyannote -Tag pyannote-collar025
# alternatif (token gerekmez; speechbrain kurulu olmalı):
pip install -r requirements.txt -r requirements-speechbrain.txt
.\scripts\diar_sweep.ps1 -Backend speechbrain -Tag speechbrain-collar025
```

Her koşu sonucu `docs/evidence/diar-results-<tarih>.jsonl`'e eklenir.

## 2026-07-02 pilot and overlap decision update

The synthetic-smoke rows above remain harness evidence only. Backend selection
uses the following metadata-only measurements from consented Turkish pilot
speech and a controlled real-voice overlap set. Raw audio, transcript, RTTM,
participant names, and speaker identities are not committed.
The comparison rows are recorded in
`docs/evidence/diar-overlap-results-2026-07-02.jsonl`.

### Pilot set

| Backend | n | Corpus DER | RTF | p50 ms | Peak VRAM delta |
|---|---:|---:|---:|---:|---:|
| pyannote 3.1 | 3 | **17.88%** | 0.026 | 1743 | 2154 MB |
| SpeechBrain ECAPA | 3 | 20.95% | **0.006** | **332** | **366 MB** |

### Controlled real-voice overlap set

| Backend | n | Audio | Corpus DER | Mean DER | Max DER | RTF | p50 ms | Peak VRAM delta |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pyannote 3.1 | 3 | 81.0 s | **20.30%** | **19.20%** | 31.31% | 0.025 | 635 | 2235 MB |
| SpeechBrain ECAPA | 3 | 81.0 s | 32.15% | 32.64% | 35.53% | **0.006** | **103** | **411 MB** |

Decision reading:

- Both candidates pass the 30% corpus DER ceiling on the pilot set.
- Only pyannote passes the same ceiling on the overlap set.
- SpeechBrain is substantially faster and lighter, so it remains a measured
  resource-constrained fallback.
- Accuracy is the #161 product wedge; therefore pyannote is the proposed
  primary self-hosted post-processing backend.

The selected pilot row is stored at
`docs/evidence/diar-decision-pilot-2026-07-02.jsonl` and is verified by
`diar_decision_gate.py`. The canonical G-WER/DER quality gate independently
passed with WER 6.47% (`n=6`, 1004 reference words) and pyannote corpus DER
17.88% (`n=3`).
