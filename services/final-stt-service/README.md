# final-stt-service

`final-stt-service`, live STT tarafından üretilen geçici metni 10-15 saniyelik
bağlam penceresiyle yeniden işler ve revize edilmiş transcript adayı üretir.
Servis istemcilere doğrudan açılmaz; Redis Streams üzerinden iç compute worker
olarak çalışır.

## #38 Kapsamı

- FastAPI health ve Prometheus metrics yüzeyleri
- Lazy-loaded faster-whisper model katmanı
- 10-15 saniye chunk sözleşmesi
- Redis Streams consumer group kalıbı
- Temel kelime-overlap segment birleştirme
- PII-safe loglama: ses/metin/session kimliği loglanmaz

## #39 Revize Durum Makinesi

Her final STT işi aynı `revisionId` altında dört sıralı olay üretir:

```text
draft -> stabilizing -> final -> revised
```

| State | Anlam |
|---|---|
| `draft` | Live STT geçici metni kabul edildi |
| `stabilizing` | 10-15 saniyelik final STT geçişi çalışıyor |
| `final` | Final modelin yalnızca mevcut chunk için ürettiği metin hazır |
| `revised` | Önceki committed metinle overlap kaldırılarak birleşmiş metin hazır |

`stateSequence` değerleri sırasıyla `0, 1, 2, 3` olur. Yalnızca `revised`
olayında `terminal=true` bulunur. Kaynak Redis işi, dört olay da başarıyla
yayımlandıktan sonra ACK edilir.

`revisionId`, `sessionId + chunkSeq + correlationId` girdilerinden
deterministik üretilir. Bir retry aynı state olaylarını yeniden yazarsa UI veya
downstream consumer `(revisionId, stateSequence)` anahtarıyla deduplikasyon
yapabilir.

`diff` alanı draft ile yeni metin arasındaki kelime bazlı değişiklikleri
`equal`, `insert`, `delete`, `replace` işlemleriyle taşır. `overlapWords`,
committed metnin sonu ile final chunk başlangıcı arasında kaldırılan kelime
sayısını belirtir.

## Geçici Model Kararı

Workcube pilot WER ölçümü henüz yapılamadığı için ADR-0031 geçici kararı
kullanılır:

```text
FINAL_STT_MODEL_NAME=large-v3
FINAL_STT_MODEL_REVISION=<approved-hugging-face-commit>
FINAL_STT_MODEL_PATH=/models/large-v3-pinned
FINAL_STT_MODEL_SHA256=<model.bin-sha256>
FINAL_STT_DEVICE=cuda
FINAL_STT_COMPUTE_TYPE=float16
FINAL_STT_BEAM_SIZE=1
```

Bu üretim model kilidi değildir. #35/#36 tamamlanınca model seçimi yeniden
değerlendirilmelidir. `staging` ve `production` ortamlarında model önceden
belirlenen revizyondan indirilmiş yerel dizinden yüklenir ve `model.bin`
SHA-256 değeri başlangıçta doğrulanır.

## Redis Mesaj Sözleşmesi

Input stream varsayılanı: `stt:final:jobs`

```json
{
  "sessionId": "opaque-session-id",
  "chunkSeq": "12",
  "audioPath": "/audio/session/chunk-12.wav",
  "audioDurationSec": "12.4",
  "committedText": "Daha önce kesinleşen metin",
  "draftText": "Canlı STT geçici metni",
  "correlationId": "request-correlation-id"
}
```

Redis'e ses byte'ı veya transcript logu yazılmaz. `audioPath`,
`FINAL_STT_AUDIO_ROOT` altında bulunmalıdır. Üretimde bu yol şifreli MinIO
objesini güvenli yerel mount/download katmanına bağlayacaktır.

Başarılı sonuç varsayılan olarak `stt:final:results` stream'ine yazılır.
Kaynak mesaj yalnızca sonuç yayınlandıktan sonra ACK edilir.

Her result stream kaydındaki `payload`, UI/downstream sözleşmesi olan bir state
event JSON'ıdır:

```json
{
  "sessionId": "opaque-session-id",
  "chunkSeq": 12,
  "correlationId": "request-correlation-id",
  "revisionId": "64-character-sha256",
  "state": "revised",
  "stateSequence": 3,
  "terminal": true,
  "text": "Birleşmiş ve revize edilmiş metin",
  "previousText": "Ekranda daha önce görülen draft metin",
  "overlapWords": 2,
  "diff": [
    {
      "operation": "replace",
      "beforeStart": 3,
      "beforeEnd": 4,
      "afterStart": 3,
      "afterEnd": 4,
      "beforeText": "yanlış",
      "afterText": "doğru"
    }
  ],
  "result": {
    "revisedText": "Birleşmiş ve revize edilmiş metin"
  }
}
```

`result` yalnızca terminal `revised` olayında bulunur; böylece birleşmiş metin
`final` aşamasında erken görünmez. Transcript ve ses yolu loglanmaz; metin
yalnızca iş akışı payload'ında taşınır.

## Yerel Çalıştırma

```powershell
cd services/final-stt-service
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

$env:FINAL_STT_REDIS_ENABLED="false"
$env:FINAL_STT_DEVICE="cpu"
$env:FINAL_STT_COMPUTE_TYPE="int8"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8211
```

GPU container varyantı #41 kapsamındadır. Bu adım GPU CUDA image'ı üretmez.

## Test

```powershell
python -m pytest -q
$env:FINAL_STT_TEST_REDIS_URL="redis://127.0.0.1:16380/0"
python -m pytest -q -m integration tests/integration/test_redis_consumer.py
python -m ruff check app tests
python -m mypy app
python -m black --check app tests
```

Docker CPU smoke:

```powershell
docker build --network=host -t final-stt-service:issue-38 .
docker run --rm -p 8211:8211 `
  -e FINAL_STT_REDIS_ENABLED=false `
  -e FINAL_STT_DEVICE=cpu `
  -e FINAL_STT_COMPUTE_TYPE=int8 `
  final-stt-service:issue-38
```

AG-019 staging resource gate pending; implementation validated locally only.
