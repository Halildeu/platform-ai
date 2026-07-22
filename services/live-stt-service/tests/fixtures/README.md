# Audio Test Fixtures

Bu dizin canonical test ses dosyaları için ayrılmıştır.

## Lisans / Kaynak Manifest (KVKK uyumlu)

| Dosya | Lisans | Kaynak | Süre | Cinsiyet/Yaş | Türkçe | Kullanım |
|---|---|---|---|---|---|---|
| `sample-tr-cv17-001.wav` | CC0 1.0 | Mozilla Common Voice 17.0 (TR test split) | ~6 sn | Hafif anonim | ✅ | Integration test smoke |
| `sample-tr-cv17-002.wav` | CC0 1.0 | Mozilla Common Voice 17.0 (TR test split) | ~8 sn | Hafif anonim | ✅ | Integration test smoke (varyans) |
| `silero-gate-fixtures.json` | Repo-generated | CC0 WAV slice + deterministic sine recipe | 3.0-3.2 sn | PII yok | ✅ | Pinned production Silero quiet-speech / non-speech counter-evidence |

**Kaynak**: https://commonvoice.mozilla.org/tr/datasets
**Lisans**: CC0 1.0 Public Domain — herhangi bir amaçla kullanılabilir, atıf gerekmez (ama bu manifest atıf yapar)
**KVKK boundary**: Anonim crowdsourced clip'ler; konuşmacı ID ve PII yok. Pilot meeting kaydı **YOK** (ADR-0030 ACCEPTED öncesi YASAK).

## Ground Truth (yaklaşık)

`sample-tr-cv17-001.txt`: Beklenen transkript (smoke assertion için)
`sample-tr-cv17-002.txt`: Beklenen transkript

`silero-gate-fixtures.json`, production speech gate davranış testinin iki girdisini
pinler. Quiet-speech girdisi `sample-tr-cv17-001.wav` dosyasının sabit zaman
aralığını RMS `0.002` seviyesine ölçekler. Above-floor non-speech girdisi sabit
frekans, süre ve RMS ile üretilen sinüs sinyalidir. İki girdi de gerçek
`faster_whisper.vad.get_speech_timestamps` yolundan geçer; yalnız Whisper decoder
test stub'ıdır. Bu test GPU/model kalite veya geniş WER iddiası üretmez.

Bunlar genel bir WER/model-kalite claim'i üretmez. Production rollout smoke'u
iki fixture'ı da ayrı ayrı source-controlled dar toleranslarla doğrular; geniş
WER kararı için aşağıdaki tam değerlendirme seti yine zorunludur.

## Tedarik

Common Voice 17 TR test split:

```bash
# HuggingFace datasets library ile
pip install datasets soundfile

python -c "
from datasets import load_dataset
import soundfile as sf
import os

ds = load_dataset('mozilla-foundation/common_voice_17_0', 'tr', split='test', streaming=True)
selected = []
for ex in ds:
    if 5 <= ex['audio']['array'].shape[0] / ex['audio']['sampling_rate'] <= 10:
        selected.append(ex)
        if len(selected) == 2:
            break

for i, ex in enumerate(selected, 1):
    sf.write(f'sample-tr-cv17-{i:03d}.wav', ex['audio']['array'], ex['audio']['sampling_rate'])
    with open(f'sample-tr-cv17-{i:03d}.txt', 'w') as f:
        f.write(ex['sentence'])
"
```

**Auth**: HF token gerekebilir (gated dataset değil ama rate limit avoid için login önerilir): `huggingface-cli login`

## WER PoC Note

Bu fixture'lar Common Voice TR'den **2 short sample** — sadece pipeline smoke / determinism / Türkçe character set verify.

Gerçek WER raporu için (PR-wer-01, M4 Accuracy):
- Common Voice TR test split full (200+ clip)
- Privacy-safe pilot meeting (explicit consent + ADR-0030 ACCEPTED sonrası)
- Triangulate: sentetik + Common Voice + pilot

3-AI mutabakat: Codex `019e8a24` REVISE → Common Voice TR minimum 1-2 + license/source manifest + no WER claim + no pilot meeting audio.
