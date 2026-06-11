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

# Operator-validated streaming tuning (PoC "best" run, 2026-06; overrides the
# ADR-0031 code defaults). Live drafts in fp16, final on full large-v3, and a
# snappier commit cadence — the deciding factor in perceived latency.
$env:STT_LIVE_MODEL_NAME = "medium"
$env:STT_LIVE_DEVICE = "cuda"
$env:STT_LIVE_COMPUTE_TYPE = "float16"
$env:STT_FINAL_MODEL_NAME = "large-v3"
$env:STT_FINAL_DEVICE = "cuda"
$env:STT_FINAL_COMPUTE_TYPE = "float16"
$env:STT_LANGUAGE = "tr"
$env:STT_LIVE_INFER_INTERVAL_MS = "700"
$env:STT_LIVE_WINDOW_SEC = "3.0"
$env:STT_FINAL_WINDOW_SEC = "8.0"
$env:STT_FORCED_COMMIT_SEC = "4.0"
$env:STT_SILENCE_RMS = "0.025"
$env:STT_MIN_SPEECH_RMS = "0.03"

if ($HfHome) {
    $env:HF_HOME = $HfHome
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $HfHome "hub"
}

# CUDA runtime DLLs (cublas/cudnn) are resolved via the user's PATH at inference
# time; SYSTEM's PATH lacks them ("Library cublas64_12.dll is not found").
# install.ps1 resolves the real dirs from the installing user's environment.
if ($CudaBin) {
    $env:Path = $CudaBin + ";" + $env:Path
}

Set-Location $svc
# Redirect via cmd.exe: uvicorn logs to stderr, and PS 5.1 *>> wraps native
# stderr lines in error records, which $ErrorActionPreference=Stop turns into
# an immediate exit on the very first INFO line.
& cmd.exe /c "`"$PythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
