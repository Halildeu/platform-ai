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
git pull
Set-ExecutionPolicy -Scope Process Bypass
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

Config `C:\ProgramData\Acik\platform-ai\meeting-ai.env` altinda olusur. OAuth
client secret ve AES-256-GCM keyring DPAPI LocalMachine ciphertext olarak tutulur;
dosya ve outbox dizini yalniz `SYSTEM` ile `BUILTIN\Administrators` ACL'ine
sahiptir. Launcher unknown/duplicate key, BOM, control character, UNC/removable
disk, reparse point, broad ACL, HTTP endpoint veya eksik keyring gorurse fail-closed
cikar. Dosya baska hosta kopyalanamaz; DPAPI blob yalniz uretildigi makinede acilir.
DPAPI optional entropy repo icinde sabittir ve ek bir parola degildir; ayni makinede
gizlilik siniri dar ACL'dir. Bu nedenle loader ACL inheritance, owner ve tum Allow
ACE'lerini SID bazinda her boot'ta yeniden dogrular.

AES key rotation eski key'leri silmeden additive yapilir:

```powershell
.\deploy\gpu-host\configure-meeting-ai.ps1 -RotateEncryptionKey
```

Her guncelleme ayni dizinde atomic replace yapar ve onceki DPAPI-protected config'i
tek `meeting-ai.env.bak` dosyasinda, ayni dar ACL ile tutar. Eski key, encrypted
outbox'ta o key ile yazilmis satir kalmadigi metadata-only DLQ/queue denetimiyle
kanitlanmadan keyring'den kaldirilmaz. PowerShell transcription/script-block logging
provisioning oturumunda kapali olmali; komut veya log secret degeri yazmaz.

Yeni config ile servis baslamazsa onceki atomik backup geri alinabilir. Restore,
mevcut config'i yeni backup yaparak iki surum arasinda geri donulebilir kalir:

```powershell
.\deploy\gpu-host\configure-meeting-ai.ps1 -RestoreBackup
```

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
.\deploy\gpu-host\update.ps1
```
`update.ps1` = `git fetch` + `reset --hard origin/main` + scheduled-task restart
(`platform-ai-live-stt` + `platform-ai-meeting-ai`). **Fail-closed**: push'lanmamış
lokal commit veya dirty tracked-tree varsa reset YAPMAZ, durur — işi önce push+PR
ile koru, sonra `-Force`. Eski `git pull` yöntemi drift ürettiği için kullanılmaz.

### Drift kontrolü (günlük, opsiyonel — read-only)
```powershell
.\deploy\gpu-host\drift-guard.ps1   # HEAD!=main / unpushed / dirty / behind → uyarı + log
```

## Kaldırma / geri alma
```powershell
.\deploy\gpu-host\install.ps1 -Uninstall
```
Rollback prosedürü: `docs/ops/warm-rollback-72h.md`.

## Dış erişim (frontend → WS)
Cloudflare quick tunnel kullanılıyorsa origin **mutlaka IPv4** verilmeli:
`cloudflared tunnel --url http://127.0.0.1:8200` (`localhost` ::1'e çözülür → connection refused).
