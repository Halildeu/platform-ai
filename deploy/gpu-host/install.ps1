# platform-ai — GPU host production install (Windows Scheduled Tasks)
# Run ONCE as Administrator on the GPU PC. Creates two auto-start tasks:
#   platform-ai-live-stt    -> live-stt-service  :8200 (cuda, ADR-0031 defaults)
#   platform-ai-meeting-ai  -> meeting-ai-service :8300 (ollama, mock fallback)
# Tasks start at machine boot (no login needed), restart on failure, and are
# started immediately at the end of this script.
#
# Re-running is safe: existing tasks are replaced (idempotent).
# Uninstall: .\install.ps1 -Uninstall

param(
    [string]$RepoRoot = "C:\platform-ai",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$tasks = @(
    @{ Name = "platform-ai-live-stt";   Script = "start-live-stt.ps1" },
    @{ Name = "platform-ai-meeting-ai"; Script = "start-meeting-ai.ps1" }
)

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell."
}

if ($Uninstall) {
    foreach ($t in $tasks) {
        try { Stop-ScheduledTask -TaskName $t.Name -ErrorAction Stop } catch {}
        try {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction Stop
            Write-Host "Removed task $($t.Name)"
        } catch {}
    }
    return
}

$deployDir = Join-Path $RepoRoot "deploy\gpu-host"
foreach ($t in $tasks) {
    if (-not (Test-Path (Join-Path $deployDir $t.Script))) {
        throw "Missing $($t.Script) under $deployDir - is RepoRoot correct?"
    }
}

# Tasks run as SYSTEM, whose PATH does not see per-user Python installs:
# resolve the full interpreter path now and bake it into the task action.
$pythonExe = (Get-Command python -ErrorAction Stop).Source
Write-Host "Using Python: $pythonExe"

# A service port already in use means a manually started instance is running;
# the task's uvicorn would fail to bind. Refuse and tell the operator.
foreach ($port in 8200, 8300) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $busyPid = ($conn | Select-Object -First 1).OwningProcess
        throw "Port $port is already in use by PID $busyPid. Stop it first: Stop-Process -Id $busyPid -Force"
    }
}

foreach ($t in $tasks) {
    $scriptPath = Join-Path $deployDir $t.Script
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -RepoRoot `"$RepoRoot`" -PythonExe `"$pythonExe`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd

    # Replace if it already exists (idempotent re-install)
    try { Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction Stop } catch {}
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $t.Name
    Write-Host "Installed + started task $($t.Name)"
}

Write-Host ""
Write-Host "Verify (wait ~30-60s for model load):"
Write-Host "  Invoke-RestMethod http://127.0.0.1:8200/health"
Write-Host "  Invoke-RestMethod http://127.0.0.1:8300/health"
Write-Host "Logs: $deployDir\logs\"
