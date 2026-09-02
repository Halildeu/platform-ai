$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $repoRoot "deploy\gpu-host\task-action-contract.ps1")
. (Join-Path $repoRoot "deploy\gpu-host\bootstrap-process.ps1")
. (Join-Path $repoRoot "deploy\gpu-host\live-stt-model-runtime.ps1")

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Test-ProcessExited {
    param([int]$ProcessId, [int]$TimeoutSec = 10)

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    do {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

# CI sets RUNNER_TEMP; a developer/GPU-host run has no such variable, so the
# suite fell over at its first Join-Path (gitops#3486 harness fix). Fall back
# to the OS temp dir — CI behaviour is byte-identical (RUNNER_TEMP wins).
$runnerTemp = $env:RUNNER_TEMP
if ([string]::IsNullOrWhiteSpace($runnerTemp)) {
    $runnerTemp = [IO.Path]::GetTempPath()
}
$fixtureRoot = Join-Path $runnerTemp ("gpu-host-bootstrap-{0}" -f `
    [Guid]::NewGuid().ToString('N'))
$childPath = Join-Path $fixtureRoot "controller-child.ps1"
$preflightResult = Join-Path $fixtureRoot "preflight.json"
$parentPidPath = Join-Path $fixtureRoot "parent.pid"
$grandchildPidPath = Join-Path $fixtureRoot "grandchild.pid"
$oldHfHome = $env:HF_HOME
$oldHubCache = $env:HUGGINGFACE_HUB_CACHE
try {
    New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
    $childSource = @'
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [switch]$NoConfirm,
    [string]$ResultPath = "",
    [int]$ExitCode = 0,
    [switch]$Hang,
    [string]$ParentPidPath = "",
    [string]$GrandchildPidPath = ""
)
$ErrorActionPreference = "Stop"
if ($NoConfirm) { $ConfirmPreference = "None" }
if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    [IO.File]::WriteAllText(
        $ResultPath,
        (@{
            major = $PSVersionTable.PSVersion.Major
            noConfirm = [bool]$NoConfirm
            whatIf = [bool]$WhatIfPreference
        } | ConvertTo-Json -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
}
if ($Hang) {
    $grandchild = Start-Process `
        -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList @("-NonInteractive", "-NoProfile", "-Command", "Start-Sleep -Seconds 300") `
        -PassThru -WindowStyle Hidden
    [IO.File]::WriteAllText($ParentPidPath, "$PID")
    [IO.File]::WriteAllText($GrandchildPidPath, "$($grandchild.Id)")
    Start-Sleep -Seconds 300
}
[void]$PSCmdlet.ShouldProcess("synthetic-controller", "validate")
exit $ExitCode
'@
    [IO.File]::WriteAllText(
        $childPath,
        $childSource,
        (New-Object Text.UTF8Encoding($false))
    )

    $preflight = Invoke-GpuHostBoundedWindowsPowerShellFile `
        -ScriptPath $childPath -ScriptArguments @(
            "-NoConfirm",
            "-ResultPath", $preflightResult,
            "-WhatIf"
        ) -TimeoutSec 30 -Operation "PS5.1 preflight binding contract"
    Assert-True ($preflight.ExitCode -eq 0) `
        "PS5.1 preflight child did not exit successfully."
    $preflightEvidence = [IO.File]::ReadAllText($preflightResult) | ConvertFrom-Json
    Assert-True ($preflightEvidence.major -eq 5) `
        "Bootstrap child did not execute under Windows PowerShell 5.1."
    Assert-True ($preflightEvidence.noConfirm -eq $true) `
        "The normal -NoConfirm script switch did not bind through -File."
    Assert-True ($preflightEvidence.whatIf -eq $true) `
        "The preflight -WhatIf common parameter did not bind through -File."

    $rollbackRan = $false
    try {
        $failure = Invoke-GpuHostBoundedWindowsPowerShellFile `
            -ScriptPath $childPath -ScriptArguments @(
                "-NoConfirm", "-ExitCode", "7"
            ) -TimeoutSec 30 -Operation "PS5.1 acceptance failure contract"
        if ($failure.ExitCode -ne 0) {
            throw "Synthetic acceptance failed with exit code $($failure.ExitCode)."
        }
    } catch {
        $rollbackRan = $true
    }
    Assert-True $rollbackRan `
        "A non-zero acceptance child did not enter bootstrap rollback handling."

    $timedOut = $false
    try {
        $null = Invoke-GpuHostBoundedWindowsPowerShellFile `
            -ScriptPath $childPath -ScriptArguments @(
                "-NoConfirm",
                "-Hang",
                "-ParentPidPath", $parentPidPath,
                "-GrandchildPidPath", $grandchildPidPath
            ) -TimeoutSec 3 -KillGraceSec 10 `
            -Operation "PS5.1 timeout rollback contract"
    } catch {
        $timedOut = $_.Exception.Message.Contains("process tree was terminated")
    }
    Assert-True $timedOut `
        "A hung controller child did not fail through bounded tree termination."
    Assert-True (Test-Path -LiteralPath $parentPidPath -PathType Leaf) `
        "Timeout fixture did not record its parent process."
    Assert-True (Test-Path -LiteralPath $grandchildPidPath -PathType Leaf) `
        "Timeout fixture did not record its grandchild process."
    $parentPid = [int][IO.File]::ReadAllText($parentPidPath)
    $grandchildPid = [int][IO.File]::ReadAllText($grandchildPidPath)
    Assert-True (Test-ProcessExited -ProcessId $parentPid) `
        "Timed-out controller parent process remained alive."
    Assert-True (Test-ProcessExited -ProcessId $grandchildPid) `
        "Timed-out controller grandchild process remained alive."

    # Synthetic artifacts keep the Windows contract small while proving that
    # staging starts from empty user/SYSTEM cache roots and produces both exact
    # revision directories before any task-install code can consume them.
    $emptyUserCache = Join-Path $fixtureRoot "empty-user-cache"
    $emptySystemCache = Join-Path $fixtureRoot "empty-system-cache"
    $sourceRoot = Join-Path $fixtureRoot "model-sources"
    $runtimeRoot = Join-Path $fixtureRoot "model-runtime"
    $policyPath = Join-Path $fixtureRoot "model-policy.json"
    New-Item -ItemType Directory -Force -Path $emptyUserCache | Out-Null
    New-Item -ItemType Directory -Force -Path $emptySystemCache | Out-Null
    $env:HF_HOME = $emptyUserCache
    $env:HUGGINGFACE_HUB_CACHE = $emptySystemCache

    $models = @()
    foreach ($role in @("live", "final")) {
        $source = Join-Path $sourceRoot $role
        New-Item -ItemType Directory -Force -Path $source | Out-Null
        [IO.File]::WriteAllBytes(
            (Join-Path $source "model.bin"),
            [Text.Encoding]::UTF8.GetBytes("synthetic-$role-model")
        )
        [IO.File]::WriteAllText(
            (Join-Path $source "config.json"),
            ("{`"role`":`"$role`"}"),
            (New-Object Text.UTF8Encoding($false))
        )
        [IO.File]::WriteAllText(
            (Join-Path $source "tokenizer.json"),
            "{`"tokens`":[]}",
            (New-Object Text.UTF8Encoding($false))
        )
        $revision = if ($role -eq "live") { "1" * 40 } else { "2" * 40 }
        $models += [ordered]@{
            role = $role
            repository = "example/synthetic-$role"
            revision = $revision
            modelBinSha256 = (Get-FileHash -LiteralPath `
                (Join-Path $source "model.bin") -Algorithm SHA256).Hash.ToLowerInvariant()
            relativePath = "artifacts/$role/$revision"
        }
    }
    [IO.File]::WriteAllText(
        $policyPath,
        ([ordered]@{
            schema = "platform-ai.live-stt.model-policy.v1"
            models = $models
        } | ConvertTo-Json -Depth 5),
        (New-Object Text.UTF8Encoding($false))
    )

    $pythonCommand = @(Get-Command python.exe -CommandType Application `
        -ErrorAction Stop | Select-Object -First 1)
    $pythonExe = [string]$pythonCommand[0].Source
    Assert-True (Test-Path -LiteralPath $pythonExe -PathType Leaf) `
        "The hosted Windows runner did not expose a usable python.exe."
    & (Join-Path $repoRoot "deploy\gpu-host\stage-live-stt-models.ps1") `
        -PythonExe $pythonExe -RuntimeRoot $runtimeRoot `
        -PolicyPath $policyPath -TestSourceRoot $sourceRoot `
        -PerModelTimeoutSec 120
    foreach ($model in $models) {
        $destination = Join-Path $runtimeRoot `
            ([string]$model.relativePath).Replace('/', '\')
        Assert-True (Test-Path -LiteralPath $destination -PathType Container) `
            "Fresh empty-cache staging missed the $($model.role) model."
        Assert-True (Test-Path -LiteralPath `
            (Join-Path $destination "integrity-manifest.json") -PathType Leaf) `
            "Fresh empty-cache staging missed the $($model.role) manifest."
    }
    $helperPath = Join-Path $repoRoot "deploy\gpu-host\stage-live-stt-model.py"
    $verified = Assert-LiveSttModelSet -RuntimeRoot $runtimeRoot `
        -PythonExe $pythonExe -PolicyPath $policyPath -HelperPath $helperPath `
        -AllowTrustedCiPath
    Assert-True (@($verified.models).Count -eq 2) `
        "Fresh empty-cache staging did not verify both model revisions."

    # An artifact outside model.bin must be detected and repaired atomically on
    # the next controlled staging run.
    $liveDestination = Join-Path $runtimeRoot `
        ([string]$models[0].relativePath).Replace('/', '\')
    [IO.File]::WriteAllText(
        (Join-Path $liveDestination "config.json"),
        "{`"role`":`"tampered`"}",
        (New-Object Text.UTF8Encoding($false))
    )
    $tamperRejected = $false
    try {
        $null = Assert-LiveSttModelSet -RuntimeRoot $runtimeRoot `
            -PythonExe $pythonExe -PolicyPath $policyPath -HelperPath $helperPath `
            -AllowTrustedCiPath
    } catch {
        $tamperRejected = $true
    }
    Assert-True $tamperRejected `
        "A non-model.bin runtime artifact change was not rejected."
    & (Join-Path $repoRoot "deploy\gpu-host\stage-live-stt-models.ps1") `
        -PythonExe $pythonExe -RuntimeRoot $runtimeRoot `
        -PolicyPath $policyPath -TestSourceRoot $sourceRoot `
        -PerModelTimeoutSec 120
    $null = Assert-LiveSttModelSet -RuntimeRoot $runtimeRoot `
        -PythonExe $pythonExe -PolicyPath $policyPath -HelperPath $helperPath `
        -AllowTrustedCiPath
} finally {
    $env:HF_HOME = $oldHfHome
    $env:HUGGINGFACE_HUB_CACHE = $oldHubCache
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force `
        -ErrorAction SilentlyContinue
}

Write-Host "gpu-host fresh bootstrap PS5.1/model staging contract: PASS"
