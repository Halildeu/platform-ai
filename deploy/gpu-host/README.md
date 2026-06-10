# GPU Host Production Deploy (Windows Scheduled Tasks)

Kalıcı deploy: GPU PC her açıldığında `live-stt-service` (:8200) ve
`meeting-ai-service` (:8300) otomatik başlar, çökerse 1 dk içinde yeniden
başlatılır. Login gerekmez (SYSTEM hesabı).

## Ön koşullar (GPU PC'de bir kez)
- Python 3.10+ PATH'te, servis bağımlılıkları kurulu
  (`pip install -r services/live-stt-service/requirements.txt` ve
  `pip install -r services/meeting-ai-service/requirements.txt`)
- CUDA sürücüsü (live-stt için), modeller ilk açılışta indirilir/cache'ten gelir
- (Opsiyonel, #54 Option B) Ollama + `ollama pull llama3.1:8b` —
  yoksa meeting-ai otomatik **mock**'a düşer, servis yine ayağa kalkar

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
Loglar: `deploy\gpu-host\logs\` (günlük dosya; **transcript-free** — KVKK #30).

## Güncelleme (yeni kod deploy etme)
```powershell
cd C:\platform-ai; git pull
Restart-ScheduledTask platform-ai-live-stt; Restart-ScheduledTask platform-ai-meeting-ai
# Restart-ScheduledTask yoksa: Stop-ScheduledTask + Start-ScheduledTask
```

## Kaldırma / geri alma
```powershell
.\deploy\gpu-host\install.ps1 -Uninstall
```
Rollback prosedürü: `docs/ops/warm-rollback-72h.md`.

## Dış erişim (frontend → WS)
Cloudflare quick tunnel kullanılıyorsa origin **mutlaka IPv4** verilmeli:
`cloudflared tunnel --url http://127.0.0.1:8200` (`localhost` ::1'e çözülür → connection refused).
