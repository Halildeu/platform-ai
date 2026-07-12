# platform-ai — meeting-ai-service starter (GPU host, production)
# Invoked by the "platform-ai-meeting-ai" Scheduled Task at machine startup.

param(
    [string]$RepoRoot = "C:\platform-ai",
    [int]$Port = 8300,
    # #54 decision: Option B (ollama). A stage/prod host must never silently
    # serve the deterministic mock when Ollama is unavailable.
    [string]$Backend = "ollama",
    [string]$OllamaHost = "http://localhost:11434",
    [string]$OllamaModel = "llama3.1:8b",
    [ValidateSet("stage", "prod")][string]$AppEnv = "stage",
    [string]$RuntimeConfigPath = "",
    # Full path required: the task runs as SYSTEM, whose PATH does not include
    # per-user Python installs. install.ps1 resolves and passes this.
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$svc = Join-Path $RepoRoot "services\meeting-ai-service"
$logDir = Join-Path $RepoRoot "deploy\gpu-host\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("meeting-ai-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

$runtimeEnv = Join-Path (Split-Path $PSCommandPath -Parent) "meeting-ai-runtime-env.ps1"
. $runtimeEnv
if ([string]::IsNullOrWhiteSpace($RuntimeConfigPath)) {
    $RuntimeConfigPath = Join-Path $env:ProgramData "Acik\platform-ai\meeting-ai.env"
}
try {
    try {
        $runtimeConfigLoaded = Import-MeetingAiRuntimeEnvironment `
            -Path $RuntimeConfigPath -Optional
    } catch {
        # Runtime-env errors are deliberately value-free. Keep the durable-delivery
        # fail-closed boundary, but leave a useful Scheduled Task diagnosis.
        Add-Content $log ("[startup] Runtime config rejected: {0}" -f $_.Exception.Message)
        throw "Meeting-ai runtime config was rejected; inspect the transcript-free service log."
    }

if ($Backend -eq "mock") {
    throw "The mock meeting-ai backend is forbidden for the GPU-host stage/prod launcher."
}

if ($Backend -eq "ollama") {
    try {
        Invoke-RestMethod -Uri "$OllamaHost/api/tags" -TimeoutSec 3 | Out-Null
    } catch {
        Add-Content $log "[startup] Ollama readiness check failed; refusing mock fallback"
        throw "Ollama readiness check failed. Scheduled Task restart policy will retry."
    }
}

# KVKK boundary: MAI_REDACT_PII stays at its default (true) and cannot be
# disabled for non-mock backends (config validator).
$env:MAI_APP_ENV = $AppEnv
if (-not $env:MAI_BACKEND) { $env:MAI_BACKEND = $Backend }
if (-not $env:MAI_OLLAMA_HOST) { $env:MAI_OLLAMA_HOST = $OllamaHost }
if (-not $env:MAI_OLLAMA_MODEL) { $env:MAI_OLLAMA_MODEL = $OllamaModel }
if (-not $env:MAI_LOG_LEVEL) { $env:MAI_LOG_LEVEL = "INFO" }
if (-not $runtimeConfigLoaded -and -not $env:MAI_INGESTION_ENABLED) {
    $env:MAI_INGESTION_ENABLED = "false"
}

# Same Intel-Fortran/MKL console-handler guard as start-live-stt.ps1: prevents a
# `forrtl: error (200) window-CLOSE` abort on schtasks /End / session close if any
# numpy/MKL-backed dependency is loaded. Harmless if the runtime is absent.
$env:FOR_DISABLE_CONSOLE_CTRL_HANDLER = "1"

Set-Location $svc
# Redirect via cmd.exe: uvicorn logs to stderr, and PS 5.1 *>> wraps native
# stderr lines in error records, which $ErrorActionPreference=Stop turns into
# an immediate exit on the very first INFO line.
    & cmd.exe /c "`"$PythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
} finally {
    # Bracket the entire post-import startup path: readiness or backend guard
    # failures must not leave the materialized plaintext client key on disk.
    Clear-MeetingAiRuntimeTlsKey
}
