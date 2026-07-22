# platform-ai — live-stt-service starter (GPU host, production)
# Invoked by the "platform-ai-live-stt" Scheduled Task at machine startup.
# Model/device defaults come from ADR-0031 (live=medium-int8, final=large-v3-turbo-fp16, cuda).

param(
    [string]$RepoRoot = "C:\platform-ai",
    [int]$Port = 8200,
    # Full path required: the task runs as SYSTEM, whose PATH does not include
    # per-user Python installs. install.ps1 resolves and passes this.
    [string]$PythonExe = "python",
    # Hardened ProgramData model root. The historical parameter name is kept
    # for Scheduled Task action compatibility; this is not a user cache.
    [string]$HfHome = "",
    # Semicolon-separated dirs containing cublas/cudnn DLLs, resolved by
    # install.ps1 from the installing user's PATH (SYSTEM cannot see them).
    [string]$CudaBin = ""
)

$ErrorActionPreference = "Stop"
$runtimeContract = Join-Path $RepoRoot "deploy\gpu-host\live-stt-runtime-contract.ps1"
if (-not (Test-Path -LiteralPath $runtimeContract -PathType Leaf)) {
    throw "Missing live-stt-runtime-contract.ps1"
}
. $runtimeContract
$runtimeEnv = Join-Path $RepoRoot "deploy\gpu-host\live-stt-runtime-env.ps1"
if (-not (Test-Path -LiteralPath $runtimeEnv -PathType Leaf)) {
    throw "Missing live-stt-runtime-env.ps1"
}
. $runtimeEnv
$taskActionContract = Join-Path $RepoRoot "deploy\gpu-host\task-action-contract.ps1"
$processContract = Join-Path $RepoRoot "deploy\gpu-host\bootstrap-process.ps1"
$modelRuntime = Join-Path $RepoRoot "deploy\gpu-host\live-stt-model-runtime.ps1"
foreach ($required in @($taskActionContract, $processContract, $modelRuntime)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Missing live STT model verification dependency: $required"
    }
}
. $taskActionContract
. $processContract
. $modelRuntime
$legacyRuntimeEnv = Join-Path $RepoRoot "deploy\gpu-host\env.local.ps1"
if (Test-Path -LiteralPath $legacyRuntimeEnv -PathType Leaf) {
    throw "Legacy plaintext env.local.ps1 detected. Migrate it to the DPAPI-backed ProgramData live-stt.env file and securely remove the legacy file before restart."
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$PythonExe = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
$HfHome = (Resolve-Path -LiteralPath $HfHome -ErrorAction Stop).Path
$svc = Join-Path $RepoRoot "services\live-stt-service"
if (-not (Test-Path -LiteralPath $svc -PathType Container) -or
    -not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) {
    throw "Live STT launcher paths do not resolve to the canonical deploy runtime."
}
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
Clear-LiveSttManagedProcessEnvironment

# Remove inherited machine/task values for immutable speech-gate policy before
# loading the source contract. The ProgramData schema never admits these keys.
foreach ($policyKey in @(
        "STT_SPEECH_GATE_PROFILE",
        "STT_SPEECH_GATE_RMS_SOURCE",
        "STT_STREAM_LIVE_VAD_FILTER",
        "STT_STREAM_FINAL_VAD_FILTER",
        "STT_STREAM_VAD_THRESHOLD",
        "STT_STREAM_VAD_MIN_SPEECH_DURATION_MS",
        "STT_STREAM_VAD_MIN_SILENCE_DURATION_MS",
        "STT_STREAM_VAD_SPEECH_PAD_MS",
        "STT_LIVE_INFER_INTERVAL_MS",
        "STT_LIVE_WINDOW_SEC",
        "STT_FINAL_WINDOW_SEC",
        "STT_FORCED_COMMIT_SEC",
        "STT_SILENCE_COMMIT_SEC",
        "STT_TAIL_OVERLAP_SEC",
        "STT_MIN_INFER_SEC"
    )) {
    [Environment]::SetEnvironmentVariable(
        $policyKey,
        $null,
        [EnvironmentVariableTarget]::Process
    )
}

# Load the mandatory source profile before optional host-local RMS overrides.
# This ordering prevents inherited Scheduled Task or machine environment from
# bypassing the profile while keeping the two bounded RMS knobs operational.
$env:STT_SPEECH_GATE_PROFILE = "$script:LiveSttSpeechGateProfile"
$env:STT_SPEECH_GATE_RMS_SOURCE = "source-baseline"
$env:STT_SILENCE_RMS = "$script:LiveSttSilenceRms"
$env:STT_MIN_SPEECH_RMS = "$script:LiveSttMinSpeechRms"
$env:STT_STREAM_LIVE_VAD_FILTER = "true"
$env:STT_STREAM_FINAL_VAD_FILTER = "true"
$env:STT_STREAM_VAD_THRESHOLD = "$script:LiveSttStreamVadThreshold"
$env:STT_STREAM_VAD_MIN_SPEECH_DURATION_MS = `
    "$script:LiveSttStreamVadMinSpeechDurationMs"
$env:STT_STREAM_VAD_MIN_SILENCE_DURATION_MS = `
    "$script:LiveSttStreamVadMinSilenceDurationMs"
$env:STT_STREAM_VAD_SPEECH_PAD_MS = "$script:LiveSttStreamVadSpeechPadMs"
$env:STT_LIVE_INFER_INTERVAL_MS = "$script:LiveSttLiveInferIntervalMs"
$env:STT_LIVE_WINDOW_SEC = "$script:LiveSttLiveWindowSec"
$env:STT_FINAL_WINDOW_SEC = "$script:LiveSttFinalWindowSec"
$env:STT_FORCED_COMMIT_SEC = "$script:LiveSttForcedCommitSec"
$env:STT_SILENCE_COMMIT_SEC = "$script:LiveSttSilenceCommitSec"
$env:STT_TAIL_OVERLAP_SEC = "$script:LiveSttTailOverlapSec"
$env:STT_MIN_INFER_SEC = "$script:LiveSttMinInferSec"

# GPU host first transcribe includes lazy Whisper model load. Historical smoke
# evidence uses a 180s budget; the default 60s can kill the worker before it ever
# warms, causing every retry to cold-start again.
$env:STT_REQUEST_TIMEOUT = "180"
$env:STT_CHUNK_CONSUMER_ENABLED = "false"
# Customer recording startup depends on both streaming models. The generic
# Settings default remains off for local/test imports; this production entry
# point opts in and exposes fail-closed readiness at /ready.
$env:STT_STREAM_PRELOAD_MODELS = "true"

if ($HfHome) {
    $env:HF_HOME = $HfHome
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $HfHome "cache"
    $env:HF_HUB_OFFLINE = "1"
    $env:HF_HUB_DISABLE_TELEMETRY = "1"
} else {
    throw "HfHome is required for the production hardened live-stt model runtime"
}

$modelPolicyPath = Join-Path $RepoRoot "deploy\gpu-host\live-stt-model-policy.json"
$modelHelperPath = Join-Path $RepoRoot "deploy\gpu-host\stage-live-stt-model.py"
$modelPolicy = Assert-LiveSttModelSet -RuntimeRoot $HfHome `
    -PythonExe $PythonExe -PolicyPath $modelPolicyPath -HelperPath $modelHelperPath
$liveModels = @($modelPolicy.models | Where-Object { $_.role -eq "live" })
$finalModels = @($modelPolicy.models | Where-Object { $_.role -eq "final" })
if ($liveModels.Count -ne 1 -or $finalModels.Count -ne 1) {
    throw "Live STT model policy did not resolve exactly one live and final model."
}
$liveModel = $liveModels[0]
$finalModel = $finalModels[0]

# Optional host-local values come only from the hardened, non-executable
# ProgramData live-stt.env file. Redis credentials are DPAPI LocalMachine blobs.
$null = Import-LiveSttRuntimeEnvironment

# Re-assert the complete source-controlled production profile AFTER host-local
# config. The data file cannot move model identity, device placement,
# quantization, preload timing, or the runtime commit accepted by the ledger.
$gitCommand = Get-Command git.exe -CommandType Application -ErrorAction Stop
$gitExe = $gitCommand.Source
$oldEap = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $runtimeCommit = @(& $gitExe -C $RepoRoot rev-parse HEAD 2> $null)
    $gitExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $oldEap
}
if ($gitExit -ne 0 -or $runtimeCommit.Count -ne 1 -or
    "$($runtimeCommit[0])".Trim().ToLowerInvariant() -notmatch '^[0-9a-f]{40}$') {
    throw "Live STT runtime commit could not be resolved from the deploy clone."
}
$env:STT_RUNTIME_COMMIT = "$($runtimeCommit[0])".Trim().ToLowerInvariant()
$env:STT_ENVIRONMENT = "production"
$env:STT_STREAM_PRELOAD_MODELS = "true"
$env:STT_STREAM_PRELOAD_MAX_ATTEMPTS = "$script:LiveSttPreloadMaxAttempts"
$env:STT_STREAM_PRELOAD_RETRY_BASE_SEC = "$script:LiveSttPreloadRetryBaseSec"
$env:STT_STREAM_MODEL_LOAD_TIMEOUT_SEC = "$script:LiveSttModelLoadTimeoutSec"
$env:STT_WORKER_KILL_GRACE_SEC = "$script:LiveSttWorkerKillGraceSec"
$env:STT_STREAM_PRELOAD_READINESS_BUDGET_SEC = "$script:LiveSttReadinessDeadlineSec"
# Profile identity, VAD roles and VAD parameters are immutable production
# policy. Do not re-assert the RMS pair here: the validated ProgramData override
# is intentionally allowed to replace only those two source baseline values.
$env:STT_SPEECH_GATE_PROFILE = "$script:LiveSttSpeechGateProfile"
$env:STT_STREAM_LIVE_VAD_FILTER = "true"
$env:STT_STREAM_FINAL_VAD_FILTER = "true"
$env:STT_STREAM_VAD_THRESHOLD = "$script:LiveSttStreamVadThreshold"
$env:STT_STREAM_VAD_MIN_SPEECH_DURATION_MS = `
    "$script:LiveSttStreamVadMinSpeechDurationMs"
$env:STT_STREAM_VAD_MIN_SILENCE_DURATION_MS = `
    "$script:LiveSttStreamVadMinSilenceDurationMs"
$env:STT_STREAM_VAD_SPEECH_PAD_MS = "$script:LiveSttStreamVadSpeechPadMs"
$env:STT_LIVE_INFER_INTERVAL_MS = "$script:LiveSttLiveInferIntervalMs"
$env:STT_LIVE_WINDOW_SEC = "$script:LiveSttLiveWindowSec"
$env:STT_FINAL_WINDOW_SEC = "$script:LiveSttFinalWindowSec"
$env:STT_FORCED_COMMIT_SEC = "$script:LiveSttForcedCommitSec"
$env:STT_SILENCE_COMMIT_SEC = "$script:LiveSttSilenceCommitSec"
$env:STT_TAIL_OVERLAP_SEC = "$script:LiveSttTailOverlapSec"
$env:STT_MIN_INFER_SEC = "$script:LiveSttMinInferSec"
$env:STT_DEVICE = "cpu"
$env:STT_COMPUTE_TYPE = "int8"
$env:STT_MODEL_NAME = [string]$liveModel.repository
$env:STT_MODEL_REVISION = [string]$liveModel.revision
$env:STT_MODEL_SHA256 = "sha256:$($liveModel.modelBinSha256)"
$env:STT_MODEL_TREE_SHA256 = "sha256:$($liveModel.treeSha256)"
$env:STT_MODEL_PATH = Join-Path $HfHome `
    ([string]$liveModel.relativePath).Replace('/', '\')
$env:STT_LIVE_MODEL_NAME = $env:STT_MODEL_NAME
$env:STT_LIVE_MODEL_REVISION = $env:STT_MODEL_REVISION
$env:STT_LIVE_MODEL_SHA256 = $env:STT_MODEL_SHA256
$env:STT_LIVE_MODEL_TREE_SHA256 = $env:STT_MODEL_TREE_SHA256
$env:STT_LIVE_MODEL_PATH = $env:STT_MODEL_PATH
$env:STT_LIVE_DEVICE = "cuda"
$env:STT_LIVE_COMPUTE_TYPE = "int8"
$env:STT_FINAL_MODEL_NAME = [string]$finalModel.repository
$env:STT_FINAL_MODEL_REVISION = [string]$finalModel.revision
$env:STT_FINAL_MODEL_SHA256 = "sha256:$($finalModel.modelBinSha256)"
$env:STT_FINAL_MODEL_TREE_SHA256 = "sha256:$($finalModel.treeSha256)"
$env:STT_FINAL_MODEL_PATH = Join-Path $HfHome `
    ([string]$finalModel.relativePath).Replace('/', '\')
$env:STT_FINAL_DEVICE = "cuda"
$env:STT_FINAL_COMPUTE_TYPE = "float16"

# CUDA runtime DLLs (cublas/cudnn) are resolved via the user's PATH at inference
# time; SYSTEM's PATH lacks them ("Library cublas64_12.dll is not found").
# install.ps1 resolves the real dirs from the installing user's environment.
if ($CudaBin) {
    $env:Path = $CudaBin + ";" + $env:Path
}

# Startup preloads both direct-stream models before /ready becomes 200. The
# updater then proves readiness, exact task/listener identity, and pinned stream
# content. There is no first-customer lazy-load fallback in this production path.

Set-Location $svc
# Redirect via cmd.exe: uvicorn logs to stderr, and PS 5.1 *>> wraps native
# stderr lines in error records, which $ErrorActionPreference=Stop turns into
# an immediate exit on the very first INFO line.
& cmd.exe /c "`"$PythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
