# PR-stt-02 Line 17 Execution Report

Date: 2026-06-04

Repo: `C:\Users\zeynep.akkilic\Desktop\platform-ai-work`

Branch: `feature/pr-stt-02-baseline-metrics`

GitHub Project item: `#17 [PR-stt-02] Memory peak + model load + transcribe latency baseline olcum`

## Kisa Karar

Bu adim plandaki amaca uygun tamamlandi.

Planin istedigi ana cikti suydu:

- Gercek TR ses fixture ile STT baseline olcumu almak.
- Model load etkisini gormek.
- Transcribe latency olcmek.
- Peak memory olcmek.
- Docker container icinde smoke test yapmak.
- Sonucu `docs/poc-stt-baseline.md` dosyasina yazmak.

Bu maddelerin hepsi yapildi ve kanitlari alindi.

## Plandan Sapma Var Mi?

Evet, iki yerde kontrollu sapma oldu. Bunlar keyfi degil, teknik engel yuzunden yapildi.

### Sapma 1: Common Voice dataset kaynagi

Planin niyeti Common Voice TR fixture kullanmakti.

Ilk hedef dataset:

```text
mozilla-foundation/common_voice_17_0
```

Sorun:

Bu dataset Hugging Face `datasets` uzerinden bu ortamda sadece metadata gibi gorundu. Audio data file bulunamadi ve script su hataya dustu:

```text
datasets.data_files.EmptyDatasetError
directory ... doesn't contain any data files
```

Yapilan cozum:

Canonical dataset once deneniyor, basarisiz olursa mirror fallback kullaniliyor:

```text
fsicoli/common_voice_17_0
```

Bu sapma planin amacini bozmaz. Cunku uretilen dosyalar yine Common Voice 17 Turkish test split kaynagindan geldi ve gercek TR wav fixture olarak kullanildi.

### Sapma 2: Docker smoke cache yolu

Plan Docker smoke istiyordu. Script ilk haliyle Windows/WSL ortaminda bozuldu.

Sorun:

`bash scripts/docker-smoke.sh` calisinca Bash tarafinda `$HOME` bazi kosullarda `/root` gibi davrandi. Bu yuzden HuggingFace cache yanlis yere baglandi:

```text
/root/.cache/huggingface
```

Container ise `stt` kullanicisi ile calisiyor. Yanlis mount ve izin sorunu yuzunden model cache yazilamadi veya model load cok uzadi. Sonuc:

```text
504 Gateway Timeout
```

Yapilan cozum:

Script Windows/WSL algilarsa Windows kullanici profilini buluyor ve HF cache'i oradan mount ediyor:

```text
C:\Users\zeynep.akkilic\.cache\huggingface
```

Bu sapma planin amacini bozmaz. Tam tersine Docker smoke'un bu PC'de tekrar edilebilir sekilde calismasini saglar.

## Plana Birebir Uyulan Maddeler

### 1. Gercek TR audio fixture

Yapildi.

Eklenen dosyalar:

```text
services/live-stt-service/tests/fixtures/sample-tr-cv17-001.wav
services/live-stt-service/tests/fixtures/sample-tr-cv17-001.txt
services/live-stt-service/tests/fixtures/sample-tr-cv17-002.wav
services/live-stt-service/tests/fixtures/sample-tr-cv17-002.txt
```

Fixture 1 beklenen metin:

```text
Gecis ulkelerinde yasananlar ise karisik.
```

Fixture 2 beklenen metin:

```text
Iki halterci Pekin'de de altin icin yarisacak.
```

Not:

Dosyalar UTF-8 tutuldugu icin `.txt` icindeki asil metin Turkce karakterlidir.

### 2. Integration test

Yapildi.

Komut:

```bash
python -m pytest -m integration -q
```

Sonuc:

```text
3 passed, 50 deselected
```

Bu, gercek audio fixture ile testlerin calistigini gosterir.

### 3. Docker build

Yapildi.

Komut:

```bash
docker build -t live-stt-service:dev .
```

Sonuc:

```text
PASS
```

### 4. Docker smoke

Yapildi.

Komut:

```bash
bash scripts/docker-smoke.sh --skip-build
```

Sonuc:

```text
Docker smoke PASS.
```

Smoke icinde dogrulananlar:

```text
/health reachable
/transcribe HTTP 200
/metrics HTTP 200
stt_transcribe_total >= 1
```

Smoke ciktisindan onemli satirlar:

```text
language: tr
duration: 5.52s
segments: 1
wall-clock: 30s
text: Gecis ulkelerinde yasananlar ise karisik.
stt_transcribe_total: 1.0
Docker smoke PASS.
```

### 5. Model load baseline

Yapildi.

Cold start olcumu:

```text
curl total time: 40.097687s
API elapsed_ms: 10872ms
approx model load + overhead: 29.226s
HTTP status: 200
```

Yorum:

Bu servis modeli lazy-load ediyor. Yani ilk `/transcribe` isteginde model yukleniyor. Bu yuzden ilk istek yavas. Planin olcmek istedigi sey buydu.

### 6. Transcribe latency baseline

Yapildi.

Warm request olcumu:

```text
curl total time: 7.718536s
API elapsed_ms: 7701ms
HTTP status: 200
```

Yorum:

Model yuklendikten sonra `medium/int8/cpu` ile yaklasik 5.5 saniyelik ses icin transcribe latency yaklasik 7.7 saniye olctuk.

Bu canli iPhone gibi deneyim hedefi degil. Bu sadece Docker CPU baseline olcumu.

### 7. Memory peak baseline

Yapildi.

Olcum:

```text
Peak observed container memory: 1.503GiB
Container memory limit: 7.753GiB
Warm post-request memory snapshot: 1.293GiB / 7.753GiB
```

Olcum yontemi:

```bash
docker stats --no-stream --format '{{.MemUsage}}'
```

### 8. Rapor dosyasi

Yapildi.

Ana baseline raporu:

```text
docs/poc-stt-baseline.md
```

Bu dosya #17'nin asil cikti dosyasidir.

Bu ek dosya ise yapilan isin ayrintili uygulama raporudur:

```text
docs/pr-stt-02-line-17-execution-report.md
```

## Yasanan Sorunlar ve Cozumler

### Sorun 1: Dataset bos gorundu

Belirti:

```text
EmptyDatasetError
```

Sebep:

`mozilla-foundation/common_voice_17_0` bu ortamda audio data dosyalarini `datasets` ile vermedi.

Cozum:

Downloader scriptine fallback dataset eklendi.

Degisen dosya:

```text
services/live-stt-service/scripts/download-cv17-tr-samples.py
```

### Sorun 2: Docker smoke script CRLF yuzunden bozuldu

Belirti:

```text
$'\r': command not found
set: pipefail\r: invalid option name
```

Sebep:

Bash script Windows satir sonu ile calisinca Linux Bash hata verdi.

Cozum:

Script LF olarak duzeltildi ve `.gitattributes` eklendi:

```text
*.sh text eol=lf
```

Degisen dosyalar:

```text
.gitattributes
services/live-stt-service/scripts/docker-smoke.sh
```

### Sorun 3: Docker icinde `requests` eksikti

Belirti:

```text
ModuleNotFoundError: No module named 'requests'
```

Sebep:

Runtime image icinde ilgili dependency yoktu.

Cozum:

`requirements.txt` icine eklendi:

```text
requests==2.32.5
```

Degisen dosya:

```text
services/live-stt-service/requirements.txt
```

### Sorun 4: Docker smoke 504 verdi

Belirti:

```text
504 Gateway Timeout
```

Sebep:

Ilk analizde model load/download sureci ve yanlis HF cache mount'u yuzunden request timeout'a dustu.

Cozum:

`docker-smoke.sh` icinde:

- Windows/WSL HF cache yolu algilandi.
- Cache dizini olusturuldu.
- Container kullanicisinin yazabilmesi icin izin toleransi eklendi.
- `STT_REQUEST_TIMEOUT` smoke icin `180` saniye yapildi.

Degisen dosya:

```text
services/live-stt-service/scripts/docker-smoke.sh
```

### Sorun 5: Structured logging ucuncu parti loglarda patladi

Belirti:

```text
KeyError: 'correlation_id'
ValueError: Formatting field not found in record: 'correlation_id'
```

Sebep:

Root logging formatinda `%(correlation_id)s` bekleniyordu. Fakat `faster_whisper`, `httpx`, `uvicorn` gibi kutuphaneler bu alani log record'a koymuyor.

Cozum:

Eksik `correlation_id` icin logging filter eklendi. Kendi uygulama loglari correlation id yazmaya devam ediyor; ucuncu parti loglarda default `-` kullaniliyor.

Degisen dosya:

```text
services/live-stt-service/app/main.py
```

### Sorun 6: Ilk logging fix yanlis yerdeydi

Belirti:

```text
KeyError: "Attempt to overwrite 'correlation_id' in LogRecord"
SyntaxError: 'return' outside function
```

Sebep:

Ilk denemede record factory yaklasimi kullandim. Bu, app'in kendi `extra={"correlation_id": ...}` alanini overwrite etmeye calisti. Sonra eski koddan artik satir kaldi.

Cozum:

Record factory kaldirildi, onun yerine `logging.Filter` kullanildi. Artik satir temizlendi ve `py_compile` ile dogrulandi.

Kontrol komutu:

```bash
python -m py_compile app/main.py app/api/transcribe.py app/services/transcribe.py
```

Sonuc:

```text
PASS
```

Bu, benim yaptigim hataydi ve duzeltildi. Final smoke test bunu da dogruladi.

## Degisen Dosyalar

Kod / script:

```text
services/live-stt-service/app/main.py
services/live-stt-service/requirements.txt
services/live-stt-service/scripts/docker-smoke.sh
services/live-stt-service/scripts/download-cv17-tr-samples.py
```

Fixture:

```text
services/live-stt-service/tests/fixtures/sample-tr-cv17-001.wav
services/live-stt-service/tests/fixtures/sample-tr-cv17-001.txt
services/live-stt-service/tests/fixtures/sample-tr-cv17-002.wav
services/live-stt-service/tests/fixtures/sample-tr-cv17-002.txt
```

Rapor / repo ayari:

```text
docs/poc-stt-baseline.md
docs/pr-stt-02-line-17-execution-report.md
.gitattributes
```

## Halil Bey Reposuna Etki

Yok.

Bu calismada:

- `upstream/Halildeu/platform-ai` icin push yapilmadi.
- PR acilmadi.
- Halil Bey'in GitHub reposuna dokunulmadi.
- Calisma sadece local fork clone uzerinde yapildi.

## GPU PoC Sistemine Etki

Yok.

Bu calismada su approved PoC klasorlerine dokunulmadi:

```text
C:\Users\denetimpc\platform-ai\services\live-stt-service
C:\Users\zeynep.akkilic\Desktop\stt-frontend
```

Bu #17 isi Docker CPU baseline isidir. Approved GPU live PoC ile karistirilmadi.

## Tehlikeli / Iliskisiz Dizinlere Etki

Yok.

Asla dokunulmamasi istenen dizinlere dokunulmadi:

```text
C:\teas
C:\teas-backend
C:\Users\zeynep.akkilic\Downloads\Toplanti Analiz Dashboardu.FIGMA
```

## Plan Uyumu Degerlendirmesi

Uyum: Yuksek.

Sebep:

- #17'nin istedigi baseline raporu olusturuldu.
- Model load, transcribe latency ve memory peak olculdu.
- Docker smoke PASS edildi.
- Integration test PASS edildi.
- Metrics counter dogrulandi.
- Gercek TR audio fixture kullanildi.

Kontrollu sapmalar:

- Dataset fallback kullanildi.
- Windows/WSL Docker cache uyumlulugu icin script iyilestirildi.
- Logging crash fix eklendi.
- Runtime dependency eksigi giderildi.

Bu sapmalar planin kapsam disina cikmak degil, #17'nin kabul kriterlerini bu PC'de calistirabilmek icin gereken destek duzeltmeleridir.

## Son Durum

#17 tamamlandi.

Bir sonraki plan maddesine gecmeden once bu degisiklikler local olarak commitlenebilir. Push yapilacaksa sadece `zeynep-serban/platform-ai` fork'una yapilmali. Halil Bey'in upstream reposuna PR/push icin ayrica onay alinmali.
