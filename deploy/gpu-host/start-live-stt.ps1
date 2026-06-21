# platform-ai — live-stt-service starter (GPU host, production)
# Invoked by the "platform-ai-live-stt" Scheduled Task at machine startup.
# Model/device defaults come from ADR-0031 (live=medium-int8, final=large-v3-turbo-fp16, cuda).

param(
    [string]$RepoRoot = "C:\platform-ai",
    [int]$Port = 8200,
    # Full path required: the task runs as SYSTEM, whose PATH does not include
    # per-user Python installs. install.ps1 resolves and passes this.
    [string]$PythonExe = "python",
    # Whisper model cache. SYSTEM's own cache is empty; install.ps1 passes the
    # installing user's ~\.cache\huggingface so models are not re-downloaded.
    [string]$HfHome = "",
    # Semicolon-separated dirs containing cublas/cudnn DLLs, resolved by
    # install.ps1 from the installing user's PATH (SYSTEM cannot see them).
    [string]$CudaBin = ""
)

$ErrorActionPreference = "Stop"
$svc = Join-Path $RepoRoot "services\live-stt-service"
$logDir = Join-Path $RepoRoot "deploy\gpu-host\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("live-stt-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# KVKK: logs are transcript-free by design (stream.py); do not enable STT_STREAM_DEBUG in prod.
$env:STT_LOG_LEVEL = "INFO"

# Streaming (live_*/final_*) already defaults to cuda per ADR-0031; the legacy
# batch /transcribe service defaults to cpu/int8, which makes /health misleading
# on a GPU host. Align it so health reflects the real device.
$env:STT_DEVICE = "cuda"
$env:STT_COMPUTE_TYPE = "float16"

if ($HfHome) {
    $env:HF_HOME = $HfHome
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $HfHome "hub"
}

# Host-local overrides (SECRETS LIVE HERE, never in the repo): if
# deploy\gpu-host\env.local.ps1 exists it is dot-sourced last, so it can set
# or override any STT_* env (e.g. STT_CHUNK_CONSUMER_ENABLED + STT_REDIS_URL
# with the Vault redis_password for the Stage-2 staging run, #151/#57).
# The file is gitignored; template: env.local.ps1.example.
$envLocal = Join-Path (Split-Path $PSCommandPath -Parent) "env.local.ps1"
if (Test-Path $envLocal) {
    . $envLocal
}

# CUDA runtime DLLs (cublas/cudnn) are resolved via the user's PATH at inference
# time; SYSTEM's PATH lacks them ("Library cublas64_12.dll is not found").
# install.ps1 resolves the real dirs from the installing user's environment.
if ($CudaBin) {
    $env:Path = $CudaBin + ";" + $env:Path
}

# Auto-warmup (#live-stt-recovery 2026-06-22): the /transcribe model is lazy-loaded
# on the first request, so a freshly (re)started or rebooted service reports
# {"status":"loading"} until something POSTs one — which is why a bare restart looks
# "stuck" forever. Kick a one-shot background warmup: wait for uvicorn to bind, then
# POST a tiny test fixture so the model loads and /health reaches "ok" with no manual
# step. Best-effort ONLY — warmup failure must never block/fail uvicorn startup, so
# the spawn is try/wrapped and the job swallows + logs everything. Disable with
# STT_STARTUP_WARMUP=0. KVKK: uses a CC0 Common Voice TR test fixture, never real
# meeting audio (review #193, Codex).
if ($env:STT_STARTUP_WARMUP -ne "0") {
    $warmupWav = Join-Path $svc "tests\fixtures\sample-tr-cv17-001.wav"
    try {
        Start-Job -Name "live-stt-warmup" -ScriptBlock {
            param($port, $wav, $log)

            function Write-WarmupLog([string]$msg) {
                try {
                    Add-Content -Path $log -Value ("[{0}] [warmup] {1}" -f (Get-Date -Format "s"), $msg) -ErrorAction SilentlyContinue
                } catch { }
            }

            try {
                for ($i = 0; $i -lt 60; $i++) {
                    Start-Sleep 5
                    try {
                        $null = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 5
                        break
                    } catch { }
                }

                if (-not (Test-Path $wav)) {
                    Write-WarmupLog "fixture missing; skipping: $wav"
                    return
                }

                $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
                if (-not $curl) {
                    Write-WarmupLog "curl.exe not found; skipping"
                    return
                }

                & curl.exe -sS --max-time 120 -F "audio=@$wav;type=audio/wav" "http://127.0.0.1:$port/transcribe?language=tr&session_id=startup-warmup&meeting_id=startup-warmup&device_id=startup-warmup" | Out-Null

                if ($LASTEXITCODE -eq 0) {
                    Write-WarmupLog "transcribe warmup completed"
                } else {
                    Write-WarmupLog "transcribe warmup curl exit=$LASTEXITCODE"
                }
            } catch {
                Write-WarmupLog ("warmup failed: " + $_.Exception.Message)
            }
        } -ArgumentList $Port, $warmupWav, $log | Out-Null
    } catch {
        try {
            Add-Content -Path $log -Value ("[{0}] [warmup] failed to start job: {1}" -f (Get-Date -Format "s"), $_.Exception.Message) -ErrorAction SilentlyContinue
        } catch { }
    }
}

Set-Location $svc
# Redirect via cmd.exe: uvicorn logs to stderr, and PS 5.1 *>> wraps native
# stderr lines in error records, which $ErrorActionPreference=Stop turns into
# an immediate exit on the very first INFO line.
& cmd.exe /c "`"$PythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
