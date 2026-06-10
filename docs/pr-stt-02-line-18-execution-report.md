# PR-stt-02 Line 18 Execution Report

Date: 2026-06-04

Repo: `C:\Users\zeynep.akkilic\Desktop\platform-ai-work`

Branch: `feature/pr-stt-02-readme-known-limits`

GitHub Project item: `#18 [PR-stt-02] README known-limits + open blocker notu`

## Kisa Karar

Bu adim plandaki amaca uygun tamamlandi.

Plan #18 kod gelistirme degil, dokumantasyon adimiydi. Amac, #17'de olculen CPU Docker baseline sonucunu ve PoC'nin acik limitlerini `platform-ai/services/live-stt-service/README.md` icine yazmakti.

## Plan #18 Ne Istiyordu?

GitHub Project #18 body:

```text
platform-ai/services/live-stt-service/README.md guncelle:
- Known limits: CPU-only, no hard kill isolation yet (M2 PR-stt-03'te), no client WebSocket, no production exposure, no GPU
- Open blocker: timeout worker leak (PR-stt-03 ile cozulecek)
- 3-AI mutabakat referansi: Codex 019e879c + Mavis 78 AGREE
```

## Yapilan Is

Sadece README guncellendi.

Guncellenen dosya:

```text
services/live-stt-service/README.md
```

Eklenen ana bolumler:

```text
## PR-stt-02 baseline
## Known limits and open blockers
```

## Neden Yapildi?

#17 sonucunda resmi repo icin CPU Docker baseline olculdu. Fakat bu baseline'in neyi temsil ettigi ve neyi temsil etmedigi acik yazilmazsa proje icinde karisiklik olabilirdi.

Ozellikle su risk vardi:

- Biri #17 sonucunu approved GPU live STT PoC sonucu sanabilir.
- Biri sync `/transcribe` endpoint'ini iPhone gibi canli dikte sistemi sanabilir.
- Biri CPU Docker PoC'yi production-ready zannedebilir.
- Timeout durumunda worker thread'in arkada devam edebilecegi bilinmezse operasyon riski saklanmis olur.

#18 bu beklenti risklerini README icinde acik hale getirmek icin yapildi.

## README'ye Eklenen Baseline Ozeti

#17'den tasinan metrikler:

```text
Model: medium
Device: cpu
Compute type: int8
Cold start total: 40.097687s
Cold API elapsed_ms: 10872ms
Approx. model load + overhead: 29.226s
Warm transcribe wall-clock: 7.718536s
Warm API elapsed_ms: 7701ms
Peak observed container memory: 1.503GiB
Docker smoke: PASS
Integration test: 3 passed, 50 deselected
```

README icinde #17 raporlarina referans verildi:

```text
docs/poc-stt-baseline.md
docs/pr-stt-02-line-17-execution-report.md
```

## README'ye Eklenen Known Limits

Planla birebir uyumlu olarak su limitler yazildi:

```text
CPU-only PoC
No GPU in this PR line
No client WebSocket
No production exposure
No hard kill isolation yet
Timeout worker leak remains open
No Redis queue consumer yet
No production WER claim
No iPhone-like live dictation claim
```

Plan body'de birebir istenmeyen ama teknik olarak gerekli olan ek aciklamalar:

```text
No Redis queue consumer yet
No production WER claim
No iPhone-like live dictation claim
Approved GPU live PoC note
```

Bu ekler planin disina cikmak icin degil, #18'in "known limits" amacini daha net ve guvenli hale getirmek icin eklendi.

## Open Blocker Notu

README'ye su blocker acik yazildi:

```text
timeout worker leak
```

Teknik anlam:

`POST /transcribe` endpoint'i `STT_REQUEST_TIMEOUT` asildiginda API tarafinda 504 donebilir. Fakat Whisper inference threadpool icinde blocking calistigi icin, alttaki worker thread inference bitene kadar yasamaya devam edebilir.

Bu neden onemli:

- Uzun veya takilan inference'larda kaynak tuketimi devam edebilir.
- Timeout API kullanicisi icin bitmis gorunse bile compute tarafinda is bitmemis olabilir.
- Bu production icin yeterli izolasyon degildir.

Planla uyumlu cozum notu:

```text
PR-stt-03'te TranscribeService multiprocessing.Process worker'a tasinmali.
Worker timeout/crash durumunda kill + respawn edilebilmeli.
```

## 3-AI Mutabakat Referansi

README'ye planin istedigi referans eklendi:

```text
Codex 019e879c + Mavis 78 AGREE
```

## Approved GPU PoC Ayrimi

README'ye ozellikle su ayrim yazildi:

```text
The separately approved GPU live STT PoC is not represented by the CPU baseline numbers above.
```

Anlami:

- #17/#18 resmi repo CPU Docker baseline'idir.
- Mudurle onay alan GPU live STT PoC baska bir akistir.
- GPU PoC'de WebSocket streaming, fast draft model ve GPU final model vardir.
- Bu README'deki sayilar o live sistemin performans sonucu degildir.
- GPU live PoC entegrasyonu daha sonraki plan maddesinde yapilmalidir.

Bu ayrim kritik cunku proje yonetiminde yanlis beklenti olusmasini engeller.

## Kod Degisti Mi?

Hayir.

Bu adimda Python/FastAPI/service/script kodu degismedi.

Sadece dokumantasyon dosyasi guncellendi:

```text
services/live-stt-service/README.md
```

## Test Calistirildi Mi?

Hayir, bu adim icin unit/integration/Docker test calistirilmadi.

Gerekce:

- #18 sadece README dokumantasyon degisikligi.
- Runtime kodu degismedi.
- #17'de zaten ilgili test kanitlari alinmisti:

```text
python -m pytest -m integration -q -> 3 passed, 50 deselected
docker build -t live-stt-service:dev . -> PASS
bash scripts/docker-smoke.sh --skip-build -> PASS
```

Bu adimda yapilan kontrol:

```text
git diff --check
```

Sonuc:

```text
PASS
```

Not:

Windows LF/CRLF uyarisi goruldu, ancak bu hata degil. `.sh` dosyalari icin onceki #17 adiminda `.gitattributes` ile LF korumasi eklendi.

## Plandan Sapma Var Mi?

Kritik sapma yok.

Planin birebir istedikleri:

| Plan maddesi | Durum |
| --- | --- |
| `README.md` guncelle | Yapildi |
| CPU-only known limit | Yapildi |
| no hard kill isolation yet | Yapildi |
| no client WebSocket | Yapildi |
| no production exposure | Yapildi |
| no GPU | Yapildi |
| timeout worker leak blocker | Yapildi |
| PR-stt-03 ile cozum notu | Yapildi |
| Codex 019e879c + Mavis 78 AGREE | Yapildi |

Kontrollu genisletmeler:

| Eklenen aciklama | Neden eklendi |
| --- | --- |
| #17 baseline tablo ozeti | README okuyucusu rapora gitmeden mevcut olcumu gorsun |
| #17 rapor linkleri | Traceability |
| No Redis queue consumer yet | Gateway/STT entegrasyonu henuz yok |
| No production WER claim | Common Voice smoke fixture accuracy benchmark sanilmasin |
| No iPhone-like live dictation claim | Sync CPU baseline live streaming sanilmasin |
| Approved GPU live PoC note | Bizim onayli GPU PoC ile resmi CPU baseline karismasin |

Bu genisletmeler #18'in amacina uygundur. Kod kapsami genisletilmedi.

## Halil Bey Reposuna Etki

Yok.

Bu calismada:

- `upstream/Halildeu/platform-ai` icin push yapilmadi.
- PR acilmadi.
- Halil Bey'in GitHub reposuna dokunulmadi.
- Calisma sadece local branch uzerinde duruyor.

## Fork Durumu

#18 su anda local branch'te:

```text
feature/pr-stt-02-readme-known-limits
```

Bu rapor yazildigi anda #18 henuz commitlenmedi ve pushlanmadi.

Commit/push istenirse guvenli hedef:

```text
origin feature/pr-stt-02-readme-known-limits
```

Yani sadece `zeynep-serban/platform-ai` fork'u.

## Sonucunda Ne Gormeliyiz?

README'yi acan biri sunlari net gormeli:

- Bu servis su anda CPU Docker PoC.
- Resmi endpoint sync `POST /transcribe`.
- Canli WebSocket client henuz yok.
- GPU bu PR hattinda yok.
- Production exposure yok.
- Timeout worker leak bilinen acik blocker.
- Bu blocker PR-stt-03'te subprocess worker ile cozulecek.
- #17 baseline olcumleri nerede.
- Approved GPU live PoC ayri entegrasyon konusu.

## Son Durum

#18 dokumantasyon isi tamamlandi.

Siradaki guvenli adim:

1. `git diff` son kez kontrol edilir.
2. Degisiklik commitlenir.
3. Istenirse sadece fork'a pushlanir.
4. Halil Bey upstream icin PR ancak ayrica onay gelirse acilir.
