# GPU Host Production Deploy (Windows Scheduled Tasks)

Kalıcı deploy: GPU PC her açıldığında `live-stt-service` (:8200) ve
`meeting-ai-service` (:8300) otomatik başlar, çökerse 1 dk içinde yeniden
başlatılır. Login gerekmez (SYSTEM hesabı).

## Ön koşullar (GPU PC'de bir kez)
- Python 3.10+ PATH'te, servis bağımlılıkları kurulu
  (`pip install -r services/live-stt-service/requirements.txt` ve
  `pip install -r services/meeting-ai-service/requirements.txt`)
- CUDA sürücüsü (live-stt için), modeller ilk açılışta indirilir/cache'ten gelir
- (#54 Option B) Ollama + `ollama pull llama3.1:8b`. Stage/prod launcher
  mock'a dusmez; Ollama yoksa fail-closed cikar ve Scheduled Task restart
  policy tekrar dener.

## Kurulum (yönetici PowerShell)
```powershell
cd C:\platform-ai
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\gpu-host\update.ps1 `
  -TargetCommit <approved-full-40-hex-commit> `
  -NoRestart -Confirm:$false
.\deploy\gpu-host\install.ps1            # RepoRoot farklıysa: -RepoRoot D:\platform-ai
```

## Doğrulama
```powershell
Invoke-RestMethod http://127.0.0.1:8200/health   # live-stt  (model load ~30-60 sn)
Invoke-RestMethod http://127.0.0.1:8300/health   # meeting-ai
Get-ScheduledTask platform-ai-*                   # ikisi de Running olmalı
```

## meeting-ai durable delivery runtime config

`meeting-ai-service` varsayilan olarak analysis delivery kapali baslar. Canonical
meeting-service persist/read zincirini acmadan once Windows hostunda elevated
PowerShell ile DPAPI-protected runtime config uretin. Client secret komut satirina,
shell history'ye veya repoya yazilmaz; script `SecureString` prompt kullanir.

```powershell
cd C:\platform-ai
.\deploy\gpu-host\configure-meeting-ai.ps1 `
  -MeetingServiceBaseUrl "https://<internal-meeting-service-origin>" `
  -MeetingServiceTokenUrl "https://<internal-auth-service-origin>/oauth2/token"
```

Kalici uretim siniri icin ayni komut mTLS malzemesini operator tarafindan dosya
yolu ile alir; private key degeri komut satirina yazilmaz:

```powershell
.\deploy\gpu-host\configure-meeting-ai.ps1 `
  -MeetingServiceBaseUrl "https://meeting-ai-gateway.internal:<port>" `
  -MeetingServiceTokenUrl "https://meeting-ai-gateway.internal:<port>/oauth2/token" `
  -TlsMode mutual `
  -TlsCaPath "C:\secure-transfer\meeting-ai-ca.pem" `
  -TlsClientCertPath "C:\secure-transfer\meeting-ai-client.pem" `
  -TlsClientKeyPath "C:\secure-transfer\meeting-ai-client.key"
```

Config `C:\ProgramData\Acik\platform-ai\meeting-ai.env` altinda olusur. OAuth
client secret ve AES-256-GCM keyring DPAPI LocalMachine ciphertext olarak tutulur;
dosya ve outbox dizini yalniz `SYSTEM` ile `BUILTIN\Administrators` ACL'ine
sahiptir. Launcher unknown/duplicate key, BOM, control character, UNC/removable
disk, reparse point, broad ACL, HTTP endpoint veya eksik keyring gorurse fail-closed
cikar. Dosya baska hosta kopyalanamaz; DPAPI blob yalniz uretildigi makinede acilir.
DPAPI optional entropy repo icinde sabittir ve ek bir parola degildir; ayni makinede
gizlilik siniri dar ACL'dir. Bu nedenle loader ACL inheritance, owner ve tum Allow
ACE'lerini SID bazinda her boot'ta yeniden dogrular.

mTLS client private key'i configte yalniz DPAPI LocalMachine ciphertext olarak
tutulur. Launcher key'i servis omru icin hardened runtime dizinine atomik olarak
materialize eder, yalniz `SYSTEM` ve `BUILTIN\Administrators` ACL'ini kabul eder ve
servis ciktiginda dosyayi temizler. CA ve client certificate public malzeme olsa da
ayni dar ACL'li runtime kokune versioned dosya olarak kopyalanir. Rotation yeni
DPAPI configini atomik yazar; Scheduled Task restart'i yeni cert/key ciftini birlikte
secer. Bu restart hem rollback hem de beklenmeyen process-kill sonrasi stale
runtime-key temizligi icin zorunlu operasyon adimidir. Servis icindeki metadata
reload'u Kubernetes Secret gibi cert/key setini atomik degistiren orchestrator'lar
icin ek kesintisiz rotation destegidir; Windows DPAPI kanalinda restart gate'i
bypass edilmez.

AES key rotation eski key'leri silmeden additive yapilir:

```powershell
.\deploy\gpu-host\configure-meeting-ai.ps1 -RotateEncryptionKey
```

Her guncelleme ayni dizinde atomic replace yapar ve onceki DPAPI-protected config'i
tek `meeting-ai.env.bak` dosyasinda, ayni dar ACL ile tutar. Komut yalniz aktif
payload DEK'ini degistirir; `MAI_INGESTION_LOOKUP_KEY_ID` ile secilen ayri HMAC
blind-index key'i sabit kalir. Servis restart'i retained outbox ve ready-inbox
satirlarini tek SQLite transaction'inda yeni aktif DEK ile yeniden sifreler. Eski
DEK, bu restart dogrulanip encrypted satirlarda eski key id kalmadigi metadata-only
denetimle kanitlanmadan keyring'den kaldirilmaz. Blind-index key rotation'i bu komutun
kapsaminda degildir; ayri versioned dual-read/backfill migration gerektirir.
PowerShell transcription/script-block logging
provisioning oturumunda kapali olmali; komut veya log secret degeri yazmaz.

Yeni config ile servis baslamazsa onceki atomik backup geri alinabilir. Restore,
mevcut config'i yeni backup yaparak iki surum arasinda geri donulebilir kalir:

```powershell
.\deploy\gpu-host\configure-meeting-ai.ps1 -RestoreBackup
```

Backup transcript-ready consumer'i acik duruma getiriyorsa eski aktivasyon izni
yeniden kullanilmaz. Hedef ortama bagli yeni, tek-kullanimlik DSSE permit; out-of-band
dogrulanmis public trust root; ve ayni trust root'un beklenen SHA-256 degeri verilmelidir:

```powershell
.\deploy\gpu-host\configure-meeting-ai.ps1 `
  -RestoreBackup `
  -ReadyPermitSourcePath C:\secure-transfer\transcript-ready-permit.dsse.json `
  -ReadyPermitTrustRootSourcePath C:\secure-transfer\transcript-ready-trust-root.json `
  -ExpectedPermitTrustRootSha256 <64-hex-trust-root-digest> `
  -PythonExe C:\platform-ai\services\meeting-ai-service\.venv\Scripts\python.exe
```

Kaynak permit atomik olarak tuketilir; hash tabanli consumption kaydi replay'i
reddeder. Public trust root tuketilmez; icerik adresli runtime kopyasi out-of-band
SHA-256 ile pinlenir. Yeni permit ve activation receipt once dar ACL'li runtime kokunde
hazirlanir, DSSE Ed25519 imzasi ve tum immutable/live binding'ler dogrulandiktan sonra
config atomik olarak degistirilir. Production private signing key hosta konmaz; Vault
Transit sinirinda kalir.

`/health` sadece senkron `/transcribe` modelinin lazy-load durumunu gosterir.
Canli urun yuzeyi icin asil readiness `/ws/stream` handshake'idir:
`loading/live_model -> loading/final_model -> ready`. `update.ps1` restart
sonrasinda bu direct stream modellerini transcript-free websocket warmup ile
yukler; bu adim basarisizsa ilk kullanici kaydi model yukleme gecikmesini oder.

Direct stream kalite smoke'u icin gelistirici makinesinden tunel acikken anonim
Common Voice TR fixture'i kullanilabilir:

```powershell
cd services\live-stt-service
python scripts\live_stream_smoke.py --url ws://127.0.0.1:18220/ws/stream
```

Bu smoke stdout'a ham audio veya transcript yazmaz; yalniz event sayisi,
latency, kelime/karakter sayisi, kisa hash ve hallucination flag gibi redacted
metrikler uretir. Gercek toplanti kaydi veya kullanici transcript'i evidence'e
konmaz.

Loglar: `deploy\gpu-host\logs\` (günlük dosya; **transcript-free** — KVKK #30).

## Güncelleme (yeni kod deploy etme) — drift-proof

> ⚠️ **Bu clone bir deploy AYNASI'dır — burada GELİŞTİRME YAPMAYIN.**
> Geliştirme ayrı bir dev clone'da yapılır → push → PR. Bu clone yalnız
> `origin/main`'i takip eder. (2026-06-21: 13 push'lanmamış commit burada
> lokal-only kaldı = single point of failure; bkz. `update.ps1`.)

```powershell
cd C:\Users\denetimpc\platform-ai
$TargetCommit = "<Project #4 evidence alanindaki approved full 40-hex commit>"
.\deploy\gpu-host\update.ps1 -TargetCommit $TargetCommit -Confirm:$false
```
`origin/main` hareketli bir discovery ref'idir; deploy artifact'i degildir. Commit,
merge edilmis PR'in tam 40-hex SHA'si olarak Project #4 evidence alanina kaydedilir.
`update.ps1` yeni remote truth'u fetch eder, target'in commit object oldugunu ve
`origin/main` soyunda kaldigini kanitlar, sonra clone'u o exact commit'te detached
HEAD'e pinler. Scheduled task restart'i bundan sonra calisir.

Script dirty tracked tree, push'lanmamis lokal commit, eksik object, ancestry
uyusmazligi, malformed/insecure ledger veya mevcut HEAD/ledger uyusmazliginda
**mutasyon yapmadan exit 2** ile durur. Override yoktur; deploy clone'daki is once
dev clone'a tasinip push + PR ile korunur. Source pin landed fakat task restart
basarisizsa ledger `restart-failed` yazar ve **exit 3** doner. Rollback mutation
veya otomatik source restore basarisizligi **exit 4**'tur.

Ledger `C:\ProgramData\Acik\platform-ai\deployment-state.json` altinda schema v1
olarak tutulur. Dizin ve dosya inheritance kapali, yalniz `SYSTEM` ve
`BUILTIN\Administrators` FullControl ACL'lidir; same-volume atomic replace ve
postcondition read-back uygulanir. Log/ledger secret veya transcript icermez.

On kontrol, source mutation yapmadan ayni object/ancestry/dirty/ledger gate'lerini
calistirir:

```powershell
.\deploy\gpu-host\update.ps1 -TargetCommit $TargetCommit -WhatIf
```

Bounded rollback operator tarafindan commit secmez; yalniz hardened ledger'daki
`previousCommit` kullanilir. Basarili rollback previous slotunu tuketir, boylece
arka arkaya rollback ile iki revision arasinda ping-pong olusmaz:

```powershell
.\deploy\gpu-host\update.ps1 -Rollback -Confirm:$false
```

Eski `git pull`, `git checkout main`, `git reset --hard origin/main` ve `-Force`
yontemleri immutable source kanitini bozdugu icin kullanilmaz.

### Drift kontrolü (günlük, opsiyonel — read-only)
```powershell
.\deploy\gpu-host\drift-guard.ps1
```

Guard moving `origin/main` ilerlediginde pinned hostu stale saymaz. Alarm
kosullari: hardened ledger gecersiz/eksik, `HEAD != currentCommit`, symbolic HEAD,
dirty tracked tree, expected object eksik veya pinned commit artik `origin/main`
soyunda degil. Basarili sonuc yalnız bu bounded source contract'ini kanitlar;
servis health, mTLS/JWT ve Electron canli kabulunun yerine gecmez.

## Kaldırma / geri alma
```powershell
.\deploy\gpu-host\install.ps1 -Uninstall
```
Rollback prosedürü: `docs/ops/warm-rollback-72h.md`.

## Dış erişim (frontend → WS)
Cloudflare quick tunnel kullanılıyorsa origin **mutlaka IPv4** verilmeli:
`cloudflared tunnel --url http://127.0.0.1:8200` (`localhost` ::1'e çözülür → connection refused).
