# ADR-0033: Diarization Yaklaşımı (Türkçe konuşmacı ayrımı)

**Durum:** DRAFT — #161 (T-B STT kalite kanıtı) PR-time kararı. Bu doküman
**karar vermez**; seçenekleri, değerlendirme eksenini ve ölçüm planını sunarak
maintainer kararını hazırlar. #161 kuralı: *"diarization yaklaşımı lisans/GPU/
Türkçe performans ölçülmeden kilitlenmez."*

## Bağlam

Diarization ("kim ne zaman konuştu") sektör standardı (Otter/Fireflies/Fathom),
**bizde gerçek ölçümlü hâli yok.** #161 bunu ürünün Türkçe-wedge'inin bir parçası
yapıyor: kanıtlı DER (Diarization Error Rate) + speaker→kişi eşleme.

**Eldeki temel (#48 iskeleti):**
- `diarization-service` ayakta: `mock` (round-robin SPEAKER_XX) + `pyannote`
  backend (`pyannote/speaker-diarization-3.1`, lazy-load, single-flight lock).
- **KVKK sınırı kodda:** yalnız anonim `SPEAKER_XX` etiketi; **voiceprint/
  embedding saklanmaz** (ADR-0030 "anonim diarizasyon" tedbiriyle uyumlu).
- **Eksik:** gerçek DER ölçümü, Türkçe değerlendirme verisi, speaker→kişi
  eşleme, GPU bütçe doğrulaması, yaklaşım kararı.

## Karar verilecek konu
Production diarization motoru hangisi olacak — ve hangi kanıtla kilitlenecek?

## Seçenekler (değerlendirme ekseni: lisans · GPU/VRAM · TR performans · KVKK)

| # | Seçenek | Lisans | VRAM (8 GB kart!) | TR / DER | KVKK | Not |
|---|---|---|---|---|---|---|
| A | **pyannote.audio 3.1** (mevcut iskelet) | MIT (kod) + gated model (HF, koşullu ticari) | ~2-4 GB ⏳ ölçülecek | ⏳ ölçülecek | ✅ voiceprint saklamadan kullanılabilir | En hazır; iskelet çalışıyor |
| B | **NVIDIA NeMo** (Sortformer/MSDD) | Apache-2.0 | ⏳ daha ağır olabilir | ⏳ ölçülecek | ✅ | Lisans temiz; entegrasyon maliyeti yüksek |
| C | **WhisperX** (VAD+pyannote wrapper) | BSD + pyannote alt-bağımlılık | A ile benzer | ⏳ | ✅ | Whisper'la sıkı bağ; alt-bağımlılık yine pyannote |
| D | **Bulut (Azure/AWS/Google diar)** | Ticari API | 0 (bulut) | İyi olabilir | 🔴 **Madde 9 yurt-dışı aktarım** | #54 Option B kararıyla **çelişir → ELE** |
| E | **Basit VAD/enerji-tabanlı** | Açık | ~0 | 🔴 düşük doğruluk | ✅ | Yetersiz; sektör paritesi-altı |

**Ön eleme:** D (bulut) KVKK ülke-içi ilkesiyle çelişir → değerlendirme dışı.
E (basit) kalite gate'ini geçemez → yalnız fallback. **Gerçek yarış: A vs B (vs C).**

## Kritik kısıt: GPU bütçesi (8 GB — 12 değil)
**Gerçek donanım RTX 4070 = 8 GB** (dokümanlardaki "12 GB" hatalı; nvidia-smi
`8188 MiB`). Mevcut yük: STT (medium+turbo ~4.6 GB) + Ollama (~5 GB) **zaten
8 GB'ı zorluyor.** Sonuç: **diarization canlı STT ile aynı anda çalışamaz** →
**toplantı-sonrası (post-processing) batch** olarak konumlandırılmalı. Bu, ADR'nin
en önemli mimari kararı; tüm seçenekler bu kısıt altında ölçülmeli.
*(Ayrı bulgu: kapasite planı 12 GB varsaymış — #40/#54 düzeltilmeli, ayrı not.)*

## Ölçüm planı (karar ancak bununla kilitlenir)
**Metrik:** DER (Diarization Error Rate) = miss + false-alarm + confusion.
**Veri (3 ayak, WER triangülasyonuyla aynı disiplin):**
1. **Açık benchmark** — Türkçe etiketli set (örn. AMI-benzeri / VoxConverse alt
   kümesi; Türkçe yoksa çok-dilli set + dürüstlük notu)
2. **Sentetik çok-konuşmacı** — bilinen konuşmacı sırasıyla birleştirilmiş CV-TR
   klipleri (ground-truth kesin; `make_synthetic_tr.py` deseni)
3. **Gerçek toplantı pilotu** — ADR-0030 ACCEPTED ile **artık açık**; consent'li
   pilot kaydı (#34 protokolü), manuel ground-truth
**Ölçülecek:** her aday için DER + VRAM peak + RTF + model-load süresi
(`wer_matrix.py` desenli yeni `diar_matrix.py`).

## speaker→kişi eşleme — KVKK gerilimi (açık tasarım kararı)
"SPEAKER_01 → Ahmet Bey" eşlemesi iki yolla olur:
- **(i) Voiceprint/embedding enrolment** → biyometrik veri → **ADR-0030 ile YASAK**
  (bu fazda voiceprint saklanmıyor). Açık rıza + ayrı KVKK değerlendirmesi ister.
- **(ii) Bağlamsal/manuel eşleme** → katılımcı listesi + konuşma sırası/manuel
  atama; biyometrik üretmez. **Önerilen güvenli yol.**
ADR bu ayrımı net çizmeli; (i) yalnız ayrı consent fazında, voiceprint-saklamasız
tekniklerle gündeme gelir.

## Önerilen taslak yol (karar Halil'in)
1. **A (pyannote 3.1) ile ölç** — iskelet hazır; HF token + `diar_matrix.py` ile
   DER/VRAM/RTF üç ayakta. B (NeMo) yalnız A gate'i geçemezse devreye.
2. **Post-processing batch** konumla (canlı değil — GPU bütçesi).
3. **speaker→kişi = (ii) bağlamsal** (voiceprint yok), (i) ayrı consent fazına ertelenir.
4. Ölçüm sonrası bu ADR ACCEPTED'a yükselir; aksi hâlde B ile tekrar.

## Açık kararlar (maintainer'a)
1. pyannote gated-model lisansı ticari pilot için yeterli mi, NeMo (Apache-2.0)
   baştan mı tercih edilsin?
2. speaker→kişi eşleme bu fazda **(ii) bağlamsal** ile sınırlı kalsın mı?
3. DER hedefi nedir (öneri: açık-benchmark ≤ %15, gerçek-toplantı pilotla kalibre)?
4. Diarization post-processing batch konumu onaylanıyor mu (canlı değil, 8 GB kısıtı)?

## Kabul kriterleri (#161 G-WER'in diarization yarısı)
- [ ] `diar_matrix.py` — DER + VRAM + RTF, ≥2 ayak (açık + sentetik) ölçülü
- [ ] Yaklaşım kararı bu ADR'de ACCEPTED + gerekçe
- [ ] speaker→kişi eşleme yolu + KVKK sınırı yazılı
- [ ] DER ≤ hedef (gerçek-toplantı ayağı pilotla teyit — G-WER)
