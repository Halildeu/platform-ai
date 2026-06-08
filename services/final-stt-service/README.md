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

Tam `draft -> stabilizing -> final -> revised` durum makinesi ve UI state
yayılımı #39 kapsamındadır.

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
python -m ruff check app tests
python -m mypy app
python -m black --check app tests
```

AG-019 staging resource gate pending; implementation validated locally only.
