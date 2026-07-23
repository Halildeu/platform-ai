# GPU Host Production Deploy (Windows Scheduled Tasks)

Kalıcı deploy: GPU PC her açıldığında `live-stt-service` (:8200) ve
`meeting-ai-service` (:8300) otomatik başlar, çökerse 1 dk içinde yeniden
başlatılır. Login gerekmez (SYSTEM hesabı).

## Ön koşullar (GPU PC'de bir kez)
- Python 3.10+ PATH'te, servis bağımlılıkları kurulu
  (`pip install -r services/live-stt-service/requirements.txt` ve
  `pip install -r services/meeting-ai-service/requirements.txt`)
- CUDA sürücüsü (live-stt için). Installer iki modeli exact revision ile indirip
  `C:\ProgramData\Acik\platform-ai\models\live-stt` altinda full-artifact
  manifest ve SYSTEM/Administrators-only ACL ile stage eder. Ilk kullaniciya
  download veya lazy-load birakilmaz.
- (#54 Option B) Ollama + `ollama pull llama3.1:8b`. Stage/prod launcher
  mock'a dusmez; Ollama yoksa fail-closed cikar ve Scheduled Task restart
  policy tekrar dener.

## Fresh bootstrap (yonetici PowerShell)

Fresh bootstrap iki ayri clone kullanir. `platform-ai-control` yalniz approved
exact commit'teki controller kodunu calistirir; Scheduled Task'lar yalniz
`platform-ai` deploy clone'undan calisir. Iki rol ayni checkout olamaz.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
$TargetCommit = "<approved-full-40-hex-commit>"
$Origin = "https://github.com/Halildeu/platform-ai.git"

git clone $Origin C:\platform-ai-control
git clone $Origin C:\platform-ai
git -C C:\platform-ai-control fetch --prune origin
git -C C:\platform-ai-control checkout --detach $TargetCommit
git -C C:\platform-ai-control reset --hard $TargetCommit
git -C C:\platform-ai fetch --prune origin
git -C C:\platform-ai checkout --detach $TargetCommit
git -C C:\platform-ai reset --hard $TargetCommit
```

Task kurmadan once secret/runtime config olusturulmalidir. En azindan stage/prod
launcher'in zorunlu `meeting-ai.env` dosyasini controller checkout'undaki
provisioner ile uretin; secret degerlerini komut satirina yazmayin:

```powershell
$meetingSecret = Read-Host "meeting-service OAuth secret" -AsSecureString
$transcriptSecret = Read-Host `
  "transcript-service capability OAuth secret" -AsSecureString
& C:\platform-ai-control\deploy\gpu-host\configure-meeting-ai.ps1 `
  -MeetingServiceBaseUrl "https://<internal-meeting-service-origin>" `
  -MeetingServiceTokenUrl "https://<internal-auth-service-origin>/oauth2/token" `
  -ClientSecret $meetingSecret `
  -TranscriptServiceBaseUrl "https://<internal-transcript-service-origin>" `
  -TranscriptServiceCapabilityPathTemplate '/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}/analysis-capability' `
  -TranscriptServiceTokenUrl "https://<internal-auth-service-origin>/oauth2/token" `
  -TranscriptServiceClientSecret $transcriptSecret
```

Redis-backed live-STT host config kullaniliyorsa DPAPI-backed
`C:\ProgramData\Acik\platform-ai\live-stt.env` dosyasini task kurulumundan once
controller checkout'undaki provisioner ile hazirlayin. Script legacy PowerShell
configini okumaz veya calistirmaz; secret yeniden `SecureString` olarak girilir:

```powershell
$redisUrl = Read-Host "live-STT Redis URL" -AsSecureString
& C:\platform-ai-control\deploy\gpu-host\configure-live-stt.ps1 `
  -RepoRoot C:\platform-ai `
  -RedisUrl $redisUrl `
  -ChunkConsumerEnabled true `
  -ChunkStreamPrefix "audio:chunks:p" `
  -ChunkPartitionCount 32 `
  -ChunkConsumerGroup "live-stt-v1" `
  -ChunkConsumerName "gpu-host-1"
```

Hostta `deploy\gpu-host\env.local.ps1` varsa ilk cagri verified DPAPI configi
yazar fakat plaintext dosya kaldigi icin fail-closed doner. Replacement secret
ve host erasure aksiyonu onaylandiktan sonra migration'i atomik config readback
sonrasinda explicit tamamlayin:

```powershell
& C:\platform-ai-control\deploy\gpu-host\configure-live-stt.ps1 `
  -RepoRoot C:\platform-ai `
  -RemoveLegacyAfterVerifiedMigration
if (Test-Path C:\platform-ai\deploy\gpu-host\env.local.ps1) {
  throw "Legacy plaintext config removal postcondition failed."
}
```

Bu islem legacy dosyayi hicbir zaman dot-source etmez. SSD/backup uzerindeki
eski bloklarin fiziksel sanitizasyonu kurumun host-erasure politikasina gore
ayrica uygulanir; updater plaintext dosya gorurse deploy'u yine reddeder.

Config hazir olduktan sonra installer yalniz controller checkout'undan
calistirilir:

```powershell
& C:\platform-ai-control\deploy\gpu-host\install.ps1 `
  -RepoRoot C:\platform-ai `
  -TargetCommit $TargetCommit
```

Installer tasklari kaydetmeden once controller updater'ini `-WhatIf` ile
calistirarak exact commit, clean checkout, ancestry ve same-origin guard'larini
dogrular. Sonra bos kullanici cache'i ve bos SYSTEM cache'inden bagimsiz olarak
iki exact model revision'ini kontrollu staging dizinine indirir; `model.bin`
pinini ve dizindeki her normal dosyanin SHA-256/size manifestini dogrular,
reparse point'leri reddeder ve runtime agacini yalniz SYSTEM ile Administrators
yazabilir hale getirir. Eksik, kismi veya yanlis revision varken task olusturmaz.

Ancak bu model kapisi gectikten sonra iki taski kaydeder ve ayni controller
`update.ps1` ile ilk start'i yapar. Child Windows PowerShell 5.1
`-NonInteractive -File ... -NoConfirm` ile ve bounded timeout ile calisir; timeout
process tree'yi sonlandirir. Bu cagrida `-NoRestart` kullanilmaz. Kabul sirasi:
startup'ta iki model preload -> `/ready` 200 ve exact `runtime_commit` -> yeni ve
stabil task/listener/runtime identity -> pinned WAV icin content-quality stream
smoke -> basari. Herhangi bir adim basarisizsa updater onceki revision'i yeniden
kabul etmeyi dener. Fresh bootstrap'ta daha once kabul edilmis revision yoksa
installer iki taski kaldirir ve 8200/8300 portlarinin birakildigini
dogrulamadan donmez.

## Kabul kanitini yeniden goruntuleme
```powershell
$ready = Invoke-RestMethod http://127.0.0.1:8200/ready
$ready.status
$ready.runtime_commit
Get-ScheduledTask platform-ai-*
Get-NetTCPConnection -LocalPort 8200,8300 -State Listen
```

## meeting-ai durable delivery runtime config

`meeting-ai-service` varsayilan olarak analysis delivery kapali baslar. Canonical
meeting-service persist/read zincirini acmadan once Windows hostunda elevated
PowerShell ile DPAPI-protected runtime config uretin. Client secret komut satirina,
shell history'ye veya repoya yazilmaz; script `SecureString` prompt kullanir.
Fresh bootstrap'ta bu provisioning task install adimindan once calistirilir;
installer eksik `meeting-ai.env` ile task yaratmaz.

```powershell
cd C:\platform-ai-control
$transcriptSecret = Read-Host `
  "transcript-service delivery capability OAuth secret" -AsSecureString
.\deploy\gpu-host\configure-meeting-ai.ps1 `
  -MeetingServiceBaseUrl "https://<internal-meeting-service-origin>" `
  -MeetingServiceTokenUrl "https://<internal-auth-service-origin>/oauth2/token" `
  -TranscriptServiceBaseUrl "https://<internal-transcript-service-origin>" `
  -TranscriptServiceCapabilityPathTemplate '/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}/analysis-capability' `
  -TranscriptServiceTokenUrl "https://<internal-auth-service-origin>/oauth2/token" `
  -TranscriptServiceClientSecret $transcriptSecret
```

Ready consumer kapali olsa da durable analysis-result teslimi her POST icin exact
meeting/session/finalization tuple'ina bagli tek-kullanimlik capability alir. Bu nedenle
transcript-service capability endpoint'i ve ayri OAuth credential her ingestion configinde
zorunludur. Rollback yalniz Redis consumer, canonical transcript read path'i ve aktivasyon
permit baglarini kaldirir; result-delivery capability credential'ini korur.

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
onceki aktif DEK'i geri secerken mevcut additive keyring'deki daha yeni DEK'leri de
dar ACL'li DPAPI configte korur. Boylece servis yeni DEK ile yeniden sifrelenmis retained
outbox/ready-inbox satirlarini okuyup tek transaction'da geri secilen aktif DEK'e tasir.
Ayni key id farkli materyal veya blind-index key ayrismasi fail-closed reddedilir. Restore,
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

`/health` sadece senkron `/transcribe` modelinin durumunu gosterir. Canli urun
yuzeyinin readiness kapisi `/ready` endpoint'idir. Production launcher once
hardened model dizinlerinin ACL ve full-artifact manifestlerini dogrular, sonra
startup lifecycle iki direct-stream modelini preload eder:
`loading/live_model -> loading/final_model -> ready`. `/ready` bu zincir, exact
runtime commit ve public `speech_gate` profile/RMS-source/VAD/cadence kanitini
tasir. `update.ps1` bu alanlar source contract ile eslesmeden kabul vermez; sonra exact
Scheduled Task, listener ve interpreter identity'sini ve pinned content smoke'u
dogrular; ilk kullanici icin lazy-load/warmup fallback'i yoktur.

Direct stream kalite smoke'u icin gelistirici makinesinden tunel acikken anonim
Common Voice TR fixture'i kullanilabilir:

```powershell
cd services\live-stt-service
python scripts\live_stream_smoke.py --url "ws://127.0.0.1:18220/ws/stream?protocol=source-ranges-v1"
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
$TargetCommit = "<Project #4 evidence alanindaki approved full 40-hex commit>"
if (-not (Test-Path C:\platform-ai-control\.git)) {
  git clone https://github.com/Halildeu/platform-ai.git C:\platform-ai-control
}
git -C C:\platform-ai-control fetch --prune origin
git -C C:\platform-ai-control checkout --detach $TargetCommit
git -C C:\platform-ai-control reset --hard $TargetCommit
& C:\platform-ai-control\deploy\gpu-host\update.ps1 `
  -RepoRoot C:\platform-ai -TargetCommit $TargetCommit -Confirm:$false
```
`origin/main` hareketli bir discovery ref'idir; deploy artifact'i degildir. Commit,
merge edilmis PR'in tam 40-hex SHA'si olarak Project #4 evidence alanina kaydedilir.
`C:\platform-ai-control` ayri, operator ACL'li kontrol clone'udur; deploy aninda
yalniz `fetch` ve exact-commit checkout icin yazilir, Scheduled Task'lar buradan
calismaz. Controller checkout'unda tracked veya untracked degisiklik varsa deploy
fail-closed olur. Controller HEAD normal deploy'da target
commit ile, rollback'te halen kabul edilmis current commit ile exact eslesmeden
updater mutasyon yapmaz. Bu ayrim ilk rollout'un eski deploy checkout'undaki
parse edilmis updater koduyla devam etmesini engeller. `update.ps1` yeni remote
truth'u fetch eder, target'in commit object oldugunu ve
`origin/main` soyunda kaldigini kanitlar, sonra clone'u o exact commit'te detached
HEAD'e pinler. Scheduled task restart'i bundan sonra calisir.

Script dirty tracked tree, push'lanmamis lokal commit, eksik object, ancestry
uyusmazligi, malformed/insecure ledger veya mevcut HEAD/ledger uyusmazliginda
**mutasyon yapmadan exit 2** ile durur. Override yoktur; deploy clone'daki is once
dev clone'a tasinip push + PR ile korunur. Source pin landed fakat task restart
basarisizsa ledger `restart-failed` yazar ve **exit 3** doner. Rollback mutation
veya otomatik source restore basarisizligi **exit 4**'tur.

HEAD ile ledger `currentCommit` out-of-band bir islem nedeniyle ayrismissa
`git checkout/reset` ile guard elle atlanmaz. Ayrı ve exact-target control
checkout'taki updater, iki commit'in de `origin/main` soyunda oldugunu ve
ledger ACL/icerigini dogruladiktan sonra trusted rollback anchor olarak yalniz
ledger `currentCommit`'ini koruyarak hedef commit'e gecebilir:

```powershell
$DeployRoot = "C:\Users\denetimpc\platform-ai"
$TargetCommit = "<approved-full-40-hex-commit>"
$ControllerCommit = "<merged-controller-full-40-hex-commit>"
.\deploy\gpu-host\update.ps1 -RepoRoot $DeployRoot `
  -TargetCommit $TargetCommit -ReconcileLedgerDrift `
  -ControllerCommit $ControllerCommit -WhatIf
.\deploy\gpu-host\update.ps1 -RepoRoot $DeployRoot `
  -TargetCommit $TargetCommit -ReconcileLedgerDrift `
  -ControllerCommit $ControllerCommit -NoConfirm
```

Recovery modu yalniz gercek bir `HEAD != currentCommit` durumunda ve mevcut
valid ledger ile calisir. Controller checkout HEAD'i
`ControllerCommit` ile birebir eslesir; bu merged commit temiz, ayni
origin'de ve `origin/main` soyunda olmalidir. Deployment `TargetCommit` bundan
bagimsiz olarak exact ve approved kalir. Gozlenen drift commit'ini ledger'a
benimsemez. Target farkliysa `previousCommit` trusted ledger `currentCommit`
olur; target zaten trusted `currentCommit` ise mevcut `previousCommit` korunur.
Ikinci komut
normal restart ve acceptance zincirini de kosar. Recovery ile `-NoRestart`
birlikte kullanilamaz. Hedef kabul edilmezse source ve ledger, gozlenen drift'e
degil onceki trusted ledger `currentCommit`/`previousCommit` ciftine geri doner.
Ilk ledger yazimi basarisiz olursa trusted source geri getirilir ve runtime
yeniden kabul edilmeden once eski ledger'in tum alanlari atomic olarak geri
yazilir ve read-back ile birebir dogrulanir. Source/ledger restore veya yeniden
kabul basarisizsa iki runtime task'i once disable edilir, calisan instance'lar
sonlandirilir ve listener yoklugu dogrulanir; bu persistent fail-closed fence
exit 4 ile raporlanir. `/End` tek basina fence degildir, cunku task restart
policy'si veya reboot servisi yeniden baslatabilir.
Pin veya acceptance sonrasindaki `lastResult` yazimi da ayni transaction
sinirindadir: yazim arizasi helper icinden cikis yapmaz; trusted rollback
denetleyicisine devredilir ve rollback sonucu kanitlanamazsa runtime fence olur.
Target checkout'tan detached-pin postcondition'ina kadarki kismi mutasyonlar da
ayni fail-closed sinirdadir: trusted source/ledger geri yuklenip runtime yeniden
kabul edilemezse task'lar disable/end edilir ve listener yoklugu dogrulanir.

Fence ancak attended bir sonraki exact immutable deploy/recovery sirasinda
acikca kaldirilir. Updater iki task'i enable edip dogrular, ardindan normal
runtime acceptance zincirini kosar; acceptance basarisizsa fence yeniden
uygulanir:

```powershell
& C:\platform-ai-control\deploy\gpu-host\update.ps1 `
  -RepoRoot C:\platform-ai -TargetCommit $TargetCommit `
  -ControllerCommit $ControllerCommit -RecoverFencedRuntime `
  -Confirm:$false
```

Ledger `C:\ProgramData\Acik\platform-ai\deployment-state.json` altinda schema v1
olarak tutulur. Dizin ve dosya inheritance kapali, yalniz `SYSTEM` ve
`BUILTIN\Administrators` FullControl ACL'lidir; same-volume atomic replace ve
postcondition read-back uygulanir. Log/ledger secret veya transcript icermez.

On kontrol, source mutation yapmadan ayni object/ancestry/dirty/ledger gate'lerini
calistirir:

```powershell
& C:\platform-ai-control\deploy\gpu-host\update.ps1 `
  -RepoRoot C:\platform-ai -TargetCommit $TargetCommit -WhatIf
```

Bounded rollback operator tarafindan commit secmez; yalniz hardened ledger'daki
`previousCommit` kullanilir. Basarili rollback previous slotunu tuketir, boylece
arka arkaya rollback ile iki revision arasinda ping-pong olusmaz:

```powershell
git -C C:\platform-ai-control fetch --prune origin
git -C C:\platform-ai-control checkout --detach $ControllerCommit
git -C C:\platform-ai-control reset --hard $ControllerCommit
& C:\platform-ai-control\deploy\gpu-host\update.ps1 `
  -RepoRoot C:\platform-ai -Rollback `
  -ControllerCommit $ControllerCommit -Confirm:$false
```

`ControllerCommit` deploy target'indan bagimsiz updater authority'sidir ve
normal deploy ile rollback'te de kullanilabilir. Boylece recovery ile eski bir
target'a donulmesi, sonraki rollback'i eski target'in updater koduna baglamaz.

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
