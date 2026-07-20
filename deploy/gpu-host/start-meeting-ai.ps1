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
    [ValidateSet("test", "stage", "prod")][string]$AppEnv = "stage",
    [string]$RuntimeConfigPath = "",
    # Full path required: the task runs as SYSTEM, whose PATH does not include
    # per-user Python installs. install.ps1 resolves and passes this.
    [string]$PythonExe = "python",
    [switch]$ValidateConfigurationOnly
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

    if (-not $runtimeConfigLoaded) {
        Clear-MeetingAiManagedProcessEnvironment
        if ($AppEnv -in @("stage", "prod")) {
            Add-Content $log "[startup] Required runtime config is unavailable"
            throw "Meeting-ai deployed launcher requires an approved runtime config."
        }
        $env:MAI_INGESTION_ENABLED = "false"
        $env:MAI_READY_CONSUMER_ENABLED = "false"
    }

    $configuredAppEnv = $env:MAI_APP_ENV
    if ($runtimeConfigLoaded -and
        -not [string]::IsNullOrWhiteSpace($configuredAppEnv) -and
        $configuredAppEnv.ToLowerInvariant() -ne $AppEnv) {
        Add-Content $log "[startup] Runtime config environment rejected"
        throw "Meeting-ai runtime config environment does not match the launcher."
    }
    # The Scheduled Task action is the authoritative deployed environment.
    # Runtime config may confirm it, but must never downgrade it.
    $env:MAI_APP_ENV = $AppEnv

    try {
        Assert-TranscriptReadyPreEnablePermit `
            -RepoRoot $RepoRoot `
            -StartupScriptPath $PSCommandPath `
            -PythonExe $PythonExe
    } catch {
        Add-Content $log "[startup] Transcript-ready pre-enable permit rejected"
        throw "Transcript-ready consumer startup permit was rejected."
    }

    if ($ValidateConfigurationOnly) {
        if ($env:CI -ne "true" -or $AppEnv -ne "test") {
            throw "Configuration-only validation is restricted to the CI test environment."
        }
        return
    }

if ($Backend -eq "mock" -and $AppEnv -in @("stage", "prod")) {
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
if (-not $env:MAI_BACKEND) { $env:MAI_BACKEND = $Backend }
if (-not $env:MAI_OLLAMA_HOST) { $env:MAI_OLLAMA_HOST = $OllamaHost }
if (-not $env:MAI_OLLAMA_MODEL) { $env:MAI_OLLAMA_MODEL = $OllamaModel }
if (-not $env:MAI_LOG_LEVEL) { $env:MAI_LOG_LEVEL = "INFO" }

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
