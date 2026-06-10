# platform-ai — meeting-ai-service starter (GPU host, production)
# Invoked by the "platform-ai-meeting-ai" Scheduled Task at machine startup.

param(
    [string]$RepoRoot = "C:\platform-ai",
    [int]$Port = 8300,
    # #54 decision: Option B (ollama). Falls back to mock automatically if the
    # Ollama server is not reachable at startup, so the service never hard-fails.
    [string]$Backend = "ollama",
    [string]$OllamaHost = "http://localhost:11434",
    [string]$OllamaModel = "llama3.1:8b",
    # Full path required: the task runs as SYSTEM, whose PATH does not include
    # per-user Python installs. install.ps1 resolves and passes this.
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$svc = Join-Path $RepoRoot "services\meeting-ai-service"
$logDir = Join-Path $RepoRoot "deploy\gpu-host\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("meeting-ai-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

if ($Backend -eq "ollama") {
    try {
        Invoke-RestMethod -Uri "$OllamaHost/api/tags" -TimeoutSec 3 | Out-Null
    } catch {
        Add-Content $log "[startup] Ollama not reachable at $OllamaHost - falling back to mock backend"
        $Backend = "mock"
    }
}

# KVKK boundary: MAI_REDACT_PII stays at its default (true) and cannot be
# disabled for non-mock backends (config validator).
$env:MAI_BACKEND = $Backend
$env:MAI_OLLAMA_HOST = $OllamaHost
$env:MAI_OLLAMA_MODEL = $OllamaModel
$env:MAI_LOG_LEVEL = "INFO"

Set-Location $svc
& $PythonExe -m uvicorn app.main:app --host 0.0.0.0 --port $Port *>> $log
