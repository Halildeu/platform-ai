# Audio Test Fixtures

Bu dizin canonical test ses dosyaları için ayrılmıştır.

## Lisans / Kaynak Manifest (KVKK uyumlu)

| Dosya | Lisans | Kaynak | Süre | Cinsiyet/Yaş | Türkçe | Kullanım |
|---|---|---|---|---|---|---|
| `sample-tr-cv17-001.wav` | CC0 1.0 | Mozilla Common Voice 17.0 (TR test split) | ~6 sn | Hafif anonim | ✅ | Integration test smoke |
| `sample-tr-cv17-002.wav` | CC0 1.0 | Mozilla Common Voice 17.0 (TR test split) | ~8 sn | Hafif anonim | ✅ | Integration test smoke (varyans) |

**Kaynak**: https://commonvoice.mozilla.org/tr/datasets
**Lisans**: CC0 1.0 Public Domain — herhangi bir amaçla kullanılabilir, atıf gerekmez (ama bu manifest atıf yapar)
**KVKK boundary**: Anonim crowdsourced clip'ler; konuşmacı ID ve PII yok. Pilot meeting kaydı **YOK** (ADR-0030 ACCEPTED öncesi YASAK).

## Ground Truth (yaklaşık)

`sample-tr-cv17-001.txt`: Beklenen transkript (smoke assertion için)
`sample-tr-cv17-002.txt`: Beklenen transkript

Bunlar WER claim için kullanılmaz — sadece pipeline çalıştığını ve Türkçe karakter desteğinin doğru olduğunu doğrulamak için.

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
- Privacy-safe pilot meeting (Workcube içi consent + ADR-0030 ACCEPTED sonrası)
- Triangulate: sentetik + Common Voice + pilot

3-AI mutabakat: Codex `019e8a24` REVISE → Common Voice TR minimum 1-2 + license/source manifest + no WER claim + no pilot meeting audio.
