# platform-ai — GPU host production install (Windows Scheduled Tasks)
# Run ONCE as Administrator on the GPU PC from a separate, clean controller
# checkout pinned to TargetCommit. Creates two auto-start tasks:
#   platform-ai-live-stt    -> live-stt-service  :8200 (cuda, ADR-0031 defaults)
#   platform-ai-meeting-ai  -> meeting-ai-service :8300 (ollama, fail-closed)
# Tasks start at machine boot (no login needed), restart on failure, and are
# started through update.ps1's full exact-commit runtime acceptance.
#
# Fresh bootstrap only. Existing tasks are never replaced by this script; use
# update.ps1 for an installed host.
# Uninstall: .\install.ps1 -Uninstall

param(
    [string]$RepoRoot = "C:\platform-ai",
    [string]$TargetCommit = "",
    [ValidateRange(30, 1800)][int]$PreflightTimeoutSec = 300,
    [ValidateRange(300, 14400)][int]$ModelStagingTimeoutSec = 7800,
    [ValidateRange(300, 3600)][int]$AcceptanceTimeoutSec = 1200,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$contractPath = Join-Path $PSScriptRoot "task-action-contract.ps1"
if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
    throw "Missing task-action-contract.ps1 next to install.ps1."
}
. $contractPath
$processContractPath = Join-Path $PSScriptRoot "bootstrap-process.ps1"
if (-not (Test-Path -LiteralPath $processContractPath -PathType Leaf)) {
    throw "Missing bootstrap-process.ps1 next to install.ps1."
}
. $processContractPath

$tasks = @(
    @{ Name = "platform-ai-live-stt";   Script = "start-live-stt.ps1" },
    @{ Name = "platform-ai-meeting-ai"; Script = "start-meeting-ai.ps1" }
)
$ports = @(8200, 8300)

function Invoke-GpuHostControllerUpdate {
    param([switch]$ValidationOnly)

    $updatePath = Join-Path $PSScriptRoot "update.ps1"
    $tokens = @(
        "-RepoRoot", $RepoRoot,
        "-TargetCommit", $TargetCommit,
        "-NoConfirm"
    )
    if ($ValidationOnly) { $tokens += "-WhatIf" }
    $timeoutSec = if ($ValidationOnly) {
        $PreflightTimeoutSec
    } else {
        $AcceptanceTimeoutSec
    }
    $operation = if ($ValidationOnly) {
        "Exact-target controller preflight"
    } else {
        "Initial exact-target runtime acceptance"
    }
    $result = Invoke-GpuHostBoundedWindowsPowerShellFile `
        -ScriptPath $updatePath -ScriptArguments $tokens `
        -TimeoutSec $timeoutSec -Operation $operation
    return $result.ExitCode
}

function Remove-GpuHostBootstrapTasks {
    foreach ($t in $tasks) {
        try { Stop-ScheduledTask -TaskName $t.Name -ErrorAction Stop } catch {}
        try {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false `
                -ErrorAction Stop
        } catch {}
    }

    $taskNamesLeft = @($tasks | Where-Object {
        $null -ne (Get-ScheduledTask -TaskName $_.Name -ErrorAction SilentlyContinue)
    } | ForEach-Object { $_.Name })
    if ($taskNamesLeft.Count -gt 0) {
        throw "Bootstrap rollback left scheduled task(s): $($taskNamesLeft -join ', ')."
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $listeners = @(foreach ($port in $ports) {
            Get-NetTCPConnection -LocalPort $port -State Listen `
                -ErrorAction SilentlyContinue
        })
        if ($listeners.Count -eq 0) { return }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)

    $listenerSummary = @($listeners | ForEach-Object {
        "port={0},pid={1}" -f $_.LocalPort, $_.OwningProcess
    }) -join "; "
    throw "Bootstrap rollback did not release service listener(s): $listenerSummary."
}

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

if ($TargetCommit.ToLowerInvariant() -notmatch '^[0-9a-f]{40}$') {
    throw "Fresh install requires TargetCommit with exactly 40 hexadecimal characters."
}
$TargetCommit = $TargetCommit.ToLowerInvariant()
$controllerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
if (Test-GpuHostSameLocalPath -Left $controllerRoot -Right $RepoRoot) {
    throw "Fresh install must run from a separate exact-target controller checkout."
}
if (-not (Test-Path -LiteralPath (Join-Path $controllerRoot ".git")) -or
    -not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) {
    throw "ControllerRoot and RepoRoot must both be git checkouts."
}
$updatePath = Join-Path $PSScriptRoot "update.ps1"
if (-not (Test-Path -LiteralPath $updatePath -PathType Leaf)) {
    throw "Missing update.ps1 in the exact-target controller checkout."
}
$meetingAiConfig = Join-Path $env:ProgramData `
    "Acik\platform-ai\meeting-ai.env"
if (-not (Test-Path -LiteralPath $meetingAiConfig -PathType Leaf)) {
    throw "Provision the DPAPI meeting-ai runtime config before installing tasks."
}

foreach ($t in $tasks) {
    if ($null -ne (Get-ScheduledTask -TaskName $t.Name `
            -ErrorAction SilentlyContinue)) {
        throw "Task $($t.Name) already exists. Use controller update.ps1, not fresh install."
    }
}

$deployDir = Join-Path $RepoRoot "deploy\gpu-host"
foreach ($t in $tasks) {
    if (-not (Test-Path (Join-Path $deployDir $t.Script))) {
        throw "Missing $($t.Script) under $deployDir - is RepoRoot correct?"
    }
}

# Tasks run as SYSTEM, whose PATH does not see per-user Python installs:
# resolve the full interpreter path now and bake it into the task action.
# Select-Object -First 1 is load-bearing: Windows commonly exposes several
# `python` entries (a real install plus the WindowsApps execution alias). Member
# enumeration over multiple matches would turn .Source into an array and bake a
# space-joined, unusable interpreter path into the persisted task action.
$pythonCommand = Get-Command python -CommandType Application `
    -ErrorAction Stop | Select-Object -First 1
$pythonExe = $pythonCommand.Source
Write-Host "Using Python: $pythonExe"

# Streaming models are staged before task creation into a fixed ProgramData
# root. The launcher verifies their full artifact manifests and ACLs before GPU
# allocation; it never depends on the installing user or SYSTEM cache.
$hfHome = Join-Path $env:ProgramData "Acik\platform-ai\models\live-stt"
Write-Host "Using hardened live-STT model runtime: $hfHome"

# CUDA runtime DLLs (cublas/cudnn) are resolved through the *user's* PATH at
# inference time; SYSTEM's PATH lacks them and the first transcribe throws
# "Library cublas64_12.dll is not found". Resolve their real directories NOW
# (we are running in the user's environment) and bake them into the task.
$cudaDirs = @()
foreach ($dll in "cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn64_8.dll", "zlibwapi.dll") {
    # cmd /c keeps where.exe's stderr out of PowerShell's error pipeline
    # (PS 5.1 + ErrorActionPreference=Stop would abort the install otherwise).
    $hits = cmd.exe /c "where $dll 2>nul"
    if ($LASTEXITCODE -eq 0 -and $hits) {
        $cudaDirs += (@($hits) | ForEach-Object { Split-Path $_ -Parent })
    }
}
# pip layout fallback: site-packages\nvidia\*\bin next to the interpreter
$nvidiaRoot = Join-Path (Split-Path $pythonExe -Parent) "Lib\site-packages\nvidia"
if (Test-Path $nvidiaRoot) {
    $cudaDirs += (Get-ChildItem -Path $nvidiaRoot -Directory |
        ForEach-Object { Join-Path $_.FullName "bin" } | Where-Object { Test-Path $_ })
}
$cudaBin = ($cudaDirs | Sort-Object -Unique) -join ";"
if (-not $cudaBin) {
    Write-Warning "No CUDA DLL dirs found (cublas64_12.dll not on PATH) - live-stt will fall back to CPU errors. Install CUDA libs or check PATH."
}
Write-Host "Using CUDA DLL dirs: $cudaBin"

# A service port already in use means a manually started instance is running;
# the task's uvicorn would fail to bind. Refuse and tell the operator.
foreach ($port in $ports) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $busyPid = ($conn | Select-Object -First 1).OwningProcess
        throw "Port $port is already in use by PID $busyPid. Stop it first: Stop-Process -Id $busyPid -Force"
    }
}

# Validate the complete immutable-source/controller contract before creating a
# task. The real call below repeats these guards before pinning and acceptance.
$preflightExit = Invoke-GpuHostControllerUpdate -ValidationOnly
if ($preflightExit -ne 0) {
    throw "Exact-target controller preflight failed with exit code $preflightExit. No task was installed."
}

# Empty-cache bootstrap is explicit: both exact revisions must be downloaded,
# staged, ACL-hardened, and full-directory verified before any task exists.
$modelStagePath = Join-Path $PSScriptRoot "stage-live-stt-models.ps1"
if (-not (Test-Path -LiteralPath $modelStagePath -PathType Leaf)) {
    throw "Missing stage-live-stt-models.ps1 in the exact-target controller checkout."
}
$modelStageResult = Invoke-GpuHostBoundedWindowsPowerShellFile `
    -ScriptPath $modelStagePath -ScriptArguments @(
        "-PythonExe", $pythonExe,
        "-RuntimeRoot", $hfHome
    ) -TimeoutSec $ModelStagingTimeoutSec `
    -Operation "Exact-revision live-STT model staging"
if ($modelStageResult.ExitCode -ne 0) {
    throw "Exact-revision model staging failed with exit code $($modelStageResult.ExitCode). No task was installed."
}

try {
    foreach ($t in $tasks) {
        $actionParams = @{
            TaskName = $t.Name
            RepoRoot = $RepoRoot
            PythonExe = $pythonExe
        }
        if ($t.Name -eq "platform-ai-live-stt") {
            $actionParams.HfHome = $hfHome
            $actionParams.CudaBin = $cudaBin
        }
        $arg = New-GpuHostTaskActionArguments @actionParams
        $action = New-ScheduledTaskAction `
            -Execute (Get-GpuHostWindowsPowerShellPath) -Argument $arg
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
            -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -DontStopOnIdleEnd

        Register-ScheduledTask -TaskName $t.Name -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "Registered task $($t.Name); acceptance has not run yet."
    }

    # Never use -NoRestart here. update.ps1 owns first start and proves /ready,
    # runtime_commit, task/listener identity, and the pinned WAV stream smoke.
    $acceptanceExit = Invoke-GpuHostControllerUpdate
    if ($acceptanceExit -ne 0) {
        throw "Initial exact-commit acceptance failed with exit code $acceptanceExit."
    }
} catch {
    $installFailure = $_.Exception.Message
    try {
        Remove-GpuHostBootstrapTasks
    } catch {
        throw "$installFailure Bootstrap no-install rollback failed: $($_.Exception.Message)"
    }
    throw "$installFailure Bootstrap rolled back to no installed tasks and released ports."
}

Write-Host ""
Write-Host "Initial exact-commit runtime acceptance succeeded."
Write-Host "  ready + runtime_commit: http://127.0.0.1:8200/ready"
Write-Host "  task/listener identity: accepted by controller update.ps1"
Write-Host "  pinned WAV stream: accepted by controller update.ps1"
Write-Host "Logs: $deployDir\logs\"
