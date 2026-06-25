# #161 + #162 Gerçek Toplantı Pilotu — WER + DER + G-INT Ölçüm Runbook

**Amaç:** #161 G-WER gate'i (STT WER + diarization DER) **ve** #162 G-INT gate'i
(özet/karar/aksiyon kalitesi) **gerçek** Türkçe konuşmayla kalibre et (sentetik
değil). Mevcut PoC üzerine (faster-whisper `medium` draft + `large-v3-turbo`
final, `/ws/stream`) + meeting-ai (`intel_eval.py`).

Tek oturumda üç ölçüm de alınabilir: aynı kayıt → WER (A) + DER (B) + G-INT (C).

## KVKK (ADR-0030 kontrollü pilot — her koşuda zorunlu)
1. **Rıza:** Her katılımcıya sor → *"Diarization/STT testi için konuşman
   kaydedilecek, ölçüm sonrası silinecek, onaylıyor musun?"* — sözlü yeterli.
2. **Nötr içerik:** Kişisel veri SÖYLETME (isim/TC/telefon/adres yok). Aşağıdaki
   nötr cümleleri veya gündelik/iş konuşması kullan.
3. **İmha:** Ölçüm biter bitmez WAV + ground-truth **sil**. Sunucuya yüklenmez;
   kayıt yalnız tarayıcıda WAV olarak iner, GPU host'ta ölçülür, silinir.

## Bölüm A — WER pilotu (TEK kişi, hemen yapılabilir)
1. **Kayıt:** stt-frontend aç → Başlat → 2-3 dk spontane Türkçe konuş (nötr) →
   Durdur → **⬇ Kaydı İndir (WAV)** → `pilot-<tarih>.wav`.
2. **Ground-truth (doğru metin):** Gerçekte ne dediğini yaz → `pilot-<tarih>.txt`
   (wav ile aynı klasör, aynı stem). Canlı transkripti referans alabilir, ama
   GERÇEK söyleneni düzelt (ground-truth = senin yazdığın).
3. **Ölç (GPU host):**
   ```powershell
   cd services\live-stt-service
   python scripts\wer.py  # ya da wer_matrix.py tek-dosya modu (aşağı)
   ```
   Pratik: `wer.py` ref+hyp alır. Hızlı yol — Python'da:
   `from wer import corpus_wer, normalize_tr; corpus_wer([(ref, hyp)])`.
4. **Sonuç** → gerçek toplantı WER'i → docs/evidence'a not + #161 yorumu.

## Bölüm B — DER pilotu (2 kişi: sen + rızalı biri)
> 2 farklı ses ŞART (diarization ayıracak şey ister). Kimlik önemsiz (anonim
> SPEAKER_xx). Rızalı herhangi biri yeterli.
1. **Kayıt:** stt-frontend → Başlat → **net SIRAYLA** konuşun (üst üste binme yok;
   sen 15sn, sonra o 15sn...). Kayıt sırasında "kim ne zaman" not alın (kolaylık).
   Durdur → WAV indir.
2. **Ground-truth (RTTM):** Kim ne zaman konuştu → `pilot-<tarih>.rttm`:
   ```
   SPEAKER pilot 1 0.000 15.000 <NA> <NA> SPEAKER_00 <NA> <NA>
   SPEAKER pilot 1 15.000 14.000 <NA> <NA> SPEAKER_01 <NA> <NA>
   ```
   (start dur formatı; SPEAKER_00 = sen, SPEAKER_01 = diğer kişi.)
3. **Ölç (GPU host, .venv-diar):** WAV+RTTM'i `tests\fixtures\diar-pilot\` koy:
   ```powershell
   cd services\diarization-service
   .\.venv-diar\Scripts\Activate.ps1
   $env:DIA_HF_TOKEN = "hf_..."   # pyannote için (env.local'dan)
   # pyannote:
   python scripts\diar_matrix.py --backend pyannote --device cuda --audio-dir tests\fixtures\diar-pilot --tag pyannote-pilot
   # speechbrain (token gerekmez):
   python scripts\diar_matrix.py --backend speechbrain --device cuda --audio-dir tests\fixtures\diar-pilot --tag speechbrain-pilot
   ```
4. **Sonuç** → gerçek DER (pyannote vs speechbrain) → ADR-0033'ü gerçek veriyle
   doldur → G-WER gate kalibre → #161 kapanışa.

> Not: `diar_matrix.py` collar **default 0.25** (dscore standardı) kullanır;
> başka değer istersen `--collar 0.0` / `--skip-overlap` ekle. Promote-grade
> koşuda `--revision <commit>` ile HF model sürümünü pinle (yeniden üretilebilirlik).

## Bölüm C — G-INT pilotu (#162 intelligence)

> Bölüm A/B ile **aynı kaydı** kullanır; ek kayıt gerekmez. Asıl değer: özet/karar/
> aksiyon çıkarımının gerçek toplantıda ne kadar isabetli + uydurmasız olduğu.

1. **Transcript:** Bölüm A'daki ground-truth metnini (gerçekte ne dendiği) kullan.
   Karar/aksiyon içermesi için 2-3 nötr karar cümlesi söylemiş ol (aşağıdaki
   örnek cümleler 2/4/6 karar-aksiyon taşır).
2. **Beklenen çıktı (ground-truth):** Bu transcript'ten beklenen karar + aksiyonları
   elle yaz → geçici lokal dosya `C:\faz24-pilot\intel-pilot-<tarih>.json`
   (`tests/fixtures` altına koyma; fixture path G-INT verifier tarafından pilot
   acceptance için reddedilir):
   ```json
   {"samples": [
     {"transcript": "<gerçek konuşma metni>",
      "expected_decisions": ["<beklenen karar>"],
      "expected_actions": ["<beklenen aksiyon>"]}
   ]}
   ```
3. **Ölç (GPU host, gerçek LLM):**
   ```powershell
   cd services\meeting-ai-service
   $env:MAI_BACKEND = "ollama"; $env:MAI_REDACT_PII = "True"
   $env:MAI_OLLAMA_MODEL = "llama3.1:8b"   # ollama list'teki model
   python scripts\intel_eval.py --eval-set C:\faz24-pilot\intel-pilot-<tarih>.json --dataset-kind pilot-meeting --tag ollama-pilot `
     > ..\..\docs\evidence\intel-eval-pilot-<tarih>.jsonl
   python scripts\gint_gate.py `
     --gint-evidence ..\..\docs\evidence\intel-eval-pilot-<tarih>.jsonl `
     --min-grounding-rate 0.95 `
     --min-action-precision 0.80 `
     --min-action-recall 0.80 `
     --min-decision-precision 0.75 `
     --min-decision-recall 0.75 `
     --max-schema-invalid-rate 0 `
     --max-format-invalid-rate 0 `
     --max-backend-error-rate 0 `
     --max-truncation-risk-rate 0 `
     --min-samples 3 `
     > ..\..\docs\evidence\gint-gate-pilot-<tarih>.json
   ```
4. **Sonuç** → gerçek G-INT (grounding rate + decision/action P/R) + verifier
   `status=pass` → #162 + ADR-0034 kalibre (sentetik-smoke yerine gerçek sayı).
   Lexical metrik olduğu unutulmasın (token-overlap, semantik değil).

## Ön hazırlık (pilot öncesi, GPU host)
- VRAM: pyannote için Ollama'yı geçici durdur (`Stop-ScheduledTask
  platform-ai-meeting-ai`); STT + pyannote 8 GB'a sığar (6.7 GB).
- Tünel + frontend hazır (cloudflared `http://127.0.0.1:8200`, `?ws=` parametresi).
- Ölçüm sonrası: WAV/RTTM/TXT **sil**, Ollama'yı geri başlat.

## Nötr test cümleleri (KVKK — kişisel veri yok)
1. "Bugünkü gündemde üç ana başlık var, sırayla ilerleyelim."
2. "Bütçe kalemlerini gözden geçirip cuma gününe kadar tamamlayalım."
3. "Yeni sürümün testleri bitti, sonuçlar beklediğimizden iyi çıktı."
4. "Bir sonraki toplantıyı haftaya aynı saatte yapabiliriz."
5. "Bu konuyu biraz daha araştırıp ekibe geri döneceğiz."
6. "Kararı netleştirdik, herkes üzerine düşeni biliyor."

## Kabul / kayıt
- WER/DER sonuçları → docs/evidence + #161 yorumu (rakam + n + koşul).
- G-INT sonucu → docs/evidence + #162 yorumu (grounding + decision/action P/R +
  `gint_gate.py` PASS envelope).
- Gerçek pilot DER, ADR-0033'ün ACCEPTED tetikleyicisini tamamlar; gerçek G-INT,
  ADR-0034'ün ACCEPTED tetikleyicisini tamamlar (mutlak değer kalibrasyonu —
  ADR-0031 WER pilot disiplininin aynısı).
- **İmha:** WAV + ground-truth (TXT/RTTM) + `intel-pilot.json` ölçüm biter bitmez
  silinir (KVKK).
