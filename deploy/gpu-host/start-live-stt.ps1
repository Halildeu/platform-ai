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
    [string]$HfHome = ""
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

Set-Location $svc
# Redirect via cmd.exe: uvicorn logs to stderr, and PS 5.1 *>> wraps native
# stderr lines in error records, which $ErrorActionPreference=Stop turns into
# an immediate exit on the very first INFO line.
& cmd.exe /c "`"$PythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
