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

# Intel-Fortran/MKL (pulled in by numpy/torch/faster-whisper) installs a console
# control handler that aborts the process with `forrtl: error (200): program
# aborting due to window-CLOSE event` when it receives a CTRL_CLOSE — which is what
# `schtasks /End`, a session logoff, or a parent-console close sends. That killed
# live-stt on restart and made fresh starts launched from an interactive session
# die immediately (root cause of the 2026-06-22 "uvicorn never came up" saga).
# Disabling the handler lets the runtime shut down normally instead of forrtl-abort.
$env:FOR_DISABLE_CONSOLE_CTRL_HANDLER = "1"

# Streaming (live_*/final_*) owns the GPU customer path. Keep the legacy batch
# /transcribe worker on CPU so an old client cannot allocate a third resident
# CUDA model after readiness and evict the preloaded recording models.
$env:STT_DEVICE = "cpu"
$env:STT_COMPUTE_TYPE = "int8"
# GPU host first transcribe includes lazy Whisper model load. Historical smoke
# evidence uses a 180s budget; the default 60s can kill the worker before it ever
# warms, causing every retry to cold-start again. env.local.ps1 can still override.
$env:STT_REQUEST_TIMEOUT = "180"
# Customer recording startup depends on both streaming models. The generic
# Settings default remains off for local/test imports; this production entry
# point opts in and exposes fail-closed readiness at /ready.
$env:STT_STREAM_PRELOAD_MODELS = "true"

if ($HfHome) {
    $env:HF_HOME = $HfHome
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $HfHome "hub"
} else {
    throw "HfHome is required for the production pinned live-stt model"
}

# Host-local overrides (SECRETS LIVE HERE, never in the repo): if
# deploy\gpu-host\env.local.ps1 exists it is dot-sourced last, so it can set
# or override runtime tuning/secret STT_* env (e.g. STT_CHUNK_CONSUMER_ENABLED + STT_REDIS_URL
# with the Vault redis_password for the Stage-2 staging run, #151/#57).
# Model identity/path pins are source-controlled and re-asserted below. The file
# is gitignored; template: env.local.ps1.example.
$envLocal = Join-Path (Split-Path $PSCommandPath -Parent) "env.local.ps1"
if (Test-Path $envLocal) {
    . $envLocal
}

# The synchronous ATS path uses the canonical Systran CTranslate2 medium
# artifact. Re-assert the source-controlled identity AFTER host-local config so
# env.local cannot silently replace an approved model. Pin the upstream
# snapshot and verify the actual model.bin bytes; neither the floating name
# "medium" nor the live-stt service version is a model version. Values are
# public artifact metadata, not credentials.
$env:STT_ENVIRONMENT = "production"
$env:STT_STREAM_PRELOAD_MODELS = "true"
$env:STT_STREAM_PRELOAD_MAX_ATTEMPTS = "2"
$env:STT_MODEL_NAME = "Systran/faster-whisper-medium"
$env:STT_MODEL_REVISION = "08e178d48790749d25932bbc082711ddcfdfbc4f"
$env:STT_MODEL_SHA256 = "sha256:9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae"
$env:STT_MODEL_PATH = Join-Path $HfHome `
    "hub\models--Systran--faster-whisper-medium\snapshots\$($env:STT_MODEL_REVISION)"
$env:STT_LIVE_MODEL_NAME = $env:STT_MODEL_NAME
$env:STT_LIVE_MODEL_REVISION = $env:STT_MODEL_REVISION
$env:STT_LIVE_MODEL_SHA256 = $env:STT_MODEL_SHA256
$env:STT_LIVE_MODEL_PATH = $env:STT_MODEL_PATH
$env:STT_FINAL_MODEL_NAME = "deepdml/faster-whisper-large-v3-turbo-ct2"
$env:STT_FINAL_MODEL_REVISION = "4df90f75321148c3a29a9e2351b7ddf8f5b115a8"
$env:STT_FINAL_MODEL_SHA256 = "sha256:e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"
$env:STT_FINAL_MODEL_PATH = Join-Path $HfHome `
    "hub\models--deepdml--faster-whisper-large-v3-turbo-ct2\snapshots\$($env:STT_FINAL_MODEL_REVISION)"

# CUDA runtime DLLs (cublas/cudnn) are resolved via the user's PATH at inference
# time; SYSTEM's PATH lacks them ("Library cublas64_12.dll is not found").
# install.ps1 resolves the real dirs from the installing user's environment.
if ($CudaBin) {
    $env:Path = $CudaBin + ";" + $env:Path
}

# NOTE (lazy model load): the /transcribe model is loaded on the FIRST request,
# so a freshly (re)started service reports {"status":"loading"} until a transcribe
# is POSTed. The deploy warmup is done by update.ps1 AFTER it restarts the task — a
# plain foreground curl, OUTSIDE this service's process tree. An in-process
# Start-Job here is NOT viable: it broke the SYSTEM-scheduled-task uvicorn launch
# under Windows PowerShell 5.1 (#193 live-acceptance failed — the MKL/torch python
# forrtl-aborts on the console event the background job triggers, so uvicorn never
# came up). A bare reboot therefore leaves the service lazy until its first real
# transcribe — the original, working behavior.

Set-Location $svc
# Redirect via cmd.exe: uvicorn logs to stderr, and PS 5.1 *>> wraps native
# stderr lines in error records, which $ErrorActionPreference=Stop turns into
# an immediate exit on the very first INFO line.
& cmd.exe /c "`"$PythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
