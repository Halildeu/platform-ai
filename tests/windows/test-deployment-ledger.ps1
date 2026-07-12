$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$updateScript = Join-Path $repoRoot "deploy\gpu-host\update.ps1"
$driftScript = Join-Path $repoRoot "deploy\gpu-host\drift-guard.ps1"
$stateModule = Join-Path $repoRoot "deploy\gpu-host\deployment-state.ps1"
$tempRoot = $env:RUNNER_TEMP
if ([string]::IsNullOrWhiteSpace($tempRoot)) { $tempRoot = $env:TEMP }
$fixtureRoot = Join-Path $tempRoot "immutable-deployment-ledger"
$remote = Join-Path $fixtureRoot "remote.git"
$source = Join-Path $fixtureRoot "source"
$deploy = Join-Path $fixtureRoot "deploy"
$statePath = Join-Path $fixtureRoot "state\deployment-state.json"
$logDir = Join-Path $fixtureRoot "logs"

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-Git {
    param([string]$WorkingDirectory, [string[]]$GitArgs)
    Push-Location $WorkingDirectory
    $oldEap = $ErrorActionPreference
    try {
        # WinPS 5.1 promotes benign native stderr (for example checkout's
        # "Switched to a new branch") to NativeCommandError when EAP=Stop.
        # Keep stdout for assertions, discard stderr, and trust the exit code.
        $ErrorActionPreference = "Continue"
        $output = @(& git @GitArgs 2> $null)
        $exitCode = $LASTEXITCODE
        $ErrorActionPreference = $oldEap
        if ($exitCode -ne 0) {
            throw "git $($GitArgs -join ' ') failed with exit $exitCode"
        }
        return $output
    } finally {
        $ErrorActionPreference = $oldEap
        Pop-Location
    }
}

function Invoke-ChildPowerShell {
    param(
        [string]$Script,
        [string[]]$ScriptArgs,
        [switch]$SuppressConfirmation
    )
    $commandTokens = @()
    foreach ($argument in $ScriptArgs) {
        if ($argument.StartsWith("-", [StringComparison]::Ordinal)) {
            $commandTokens += $argument
        } else {
            $commandTokens += ("'{0}'" -f $argument.Replace("'", "''"))
        }
    }
    $confirmToken = ""
    if ($SuppressConfirmation) { $confirmToken = " -Confirm:`$false" }
    $command = "& '{0}' {1}{2}" -f `
        $Script.Replace("'", "''"), ($commandTokens -join " "), $confirmToken
    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -Command $command 2>&1)
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output = $output
        }
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Invoke-Update {
    param([string[]]$ExtraArgs)
    $arguments = @(
        "-RepoRoot", $deploy,
        "-StatePath", $statePath,
        "-Branch", "main"
    ) + $ExtraArgs
    return Invoke-ChildPowerShell -Script $updateScript -ScriptArgs $arguments `
        -SuppressConfirmation
}

function Invoke-Drift {
    return Invoke-ChildPowerShell -Script $driftScript -ScriptArgs @(
        "-RepoRoot", $deploy,
        "-StatePath", $statePath,
        "-LogDir", $logDir,
        "-Branch", "main"
    )
}

function New-SourceCommit {
    param([string]$Name, [string]$Content)
    [IO.File]::WriteAllText(
        (Join-Path $source $Name),
        $Content,
        (New-Object Text.UTF8Encoding($false))
    )
    Invoke-Git $source @("add", $Name) | Out-Null
    Invoke-Git $source @("commit", "-m", "fixture $Name") | Out-Null
    Invoke-Git $source @("push", "origin", "main") | Out-Null
    return "$(Invoke-Git $source @('rev-parse', 'HEAD'))".Trim().ToLowerInvariant()
}

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Assert-True $isAdmin `
    "immutable ledger ACL behavior test requires an elevated Windows runner"

if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $fixtureRoot | Out-Null

& git init --bare $remote | Out-Null
if ($LASTEXITCODE -ne 0) { throw "bare remote init failed" }
& git clone $remote $source | Out-Null
if ($LASTEXITCODE -ne 0) { throw "source clone failed" }
Invoke-Git $source @("config", "user.email", "ci@example.invalid") | Out-Null
Invoke-Git $source @("config", "user.name", "CI Fixture") | Out-Null
Invoke-Git $source @("checkout", "-b", "main") | Out-Null
$commitA = New-SourceCommit -Name "a.txt" -Content "A"

& git clone --branch main $remote $deploy | Out-Null
if ($LASTEXITCODE -ne 0) { throw "deploy clone failed" }
Invoke-Git $deploy @("config", "user.email", "ci@example.invalid") | Out-Null
Invoke-Git $deploy @("config", "user.name", "CI Fixture") | Out-Null

$commitB = New-SourceCommit -Name "b.txt" -Content "B"
$first = Invoke-Update @("-TargetCommit", $commitB, "-NoRestart")
Assert-True ($first.ExitCode -eq 0) (
    "first immutable deploy failed: exit={0}; output={1}" -f `
    $first.ExitCode, ($first.Output -join " | ")
)
. $stateModule
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitB) "first deploy currentCommit mismatch"
Assert-True ([string]::IsNullOrWhiteSpace($state.previousCommit)) `
    "first deploy must not invent previousCommit"
Assert-True ($state.lastResult -eq "pinned-no-restart") `
    "first deploy result mismatch"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitB) `
    "first deploy HEAD mismatch"
Push-Location $deploy
try {
    & git symbolic-ref -q HEAD *> $null
    Assert-True ($LASTEXITCODE -ne 0) "immutable deploy HEAD must be detached"
} finally { Pop-Location }

$commitC = New-SourceCommit -Name "c.txt" -Content "C"
$second = Invoke-Update @("-TargetCommit", $commitC, "-NoRestart")
Assert-True ($second.ExitCode -eq 0) "second immutable deploy failed"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) "second deploy currentCommit mismatch"
Assert-True ($state.previousCommit -eq $commitB) "second deploy previousCommit mismatch"

$commitD = New-SourceCommit -Name "d.txt" -Content "D"
$whatIf = Invoke-Update @("-TargetCommit", $commitD, "-NoRestart", "-WhatIf")
Assert-True ($whatIf.ExitCode -eq 0) "WhatIf validation failed"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) "WhatIf mutated ledger"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
    "WhatIf mutated HEAD"

$short = Invoke-Update @("-TargetCommit", $commitD.Substring(0, 12), `
    "-NoRestart")
Assert-True ($short.ExitCode -eq 2) "short commit must fail with guard exit 2"

[IO.File]::WriteAllText((Join-Path $deploy "c.txt"), "dirty")
$dirty = Invoke-Update @("-TargetCommit", $commitD, "-NoRestart")
Assert-True ($dirty.ExitCode -eq 2) "dirty tracked tree must fail with guard exit 2"
Invoke-Git $deploy @("reset", "--hard", $commitC) | Out-Null

Invoke-Git $source @("checkout", "-b", "side", $commitA) | Out-Null
[IO.File]::WriteAllText((Join-Path $source "side.txt"), "side")
Invoke-Git $source @("add", "side.txt") | Out-Null
Invoke-Git $source @("commit", "-m", "side commit") | Out-Null
$sideCommit = "$(Invoke-Git $source @('rev-parse', 'HEAD'))".Trim().ToLowerInvariant()
Invoke-Git $source @("push", "origin", "side") | Out-Null
Invoke-Git $source @("checkout", "main") | Out-Null
$notAncestor = Invoke-Update @("-TargetCommit", $sideCommit, "-NoRestart")
Assert-True ($notAncestor.ExitCode -eq 2) `
    "non-main-ancestor commit must fail with guard exit 2"

Invoke-Git $deploy @("checkout", "-b", "local-only", $commitC) | Out-Null
[IO.File]::WriteAllText((Join-Path $deploy "local.txt"), "local")
Invoke-Git $deploy @("add", "local.txt") | Out-Null
Invoke-Git $deploy @("commit", "-m", "local only") | Out-Null
$unpushed = Invoke-Update @("-TargetCommit", $commitD, "-NoRestart")
Assert-True ($unpushed.ExitCode -eq 2) "unpushed commit must fail with guard exit 2"
Invoke-Git $deploy @("checkout", "--detach", $commitC) | Out-Null

$rollback = Invoke-Update @("-Rollback", "-NoRestart")
Assert-True ($rollback.ExitCode -eq 0) "bounded rollback failed"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitB) "rollback currentCommit mismatch"
Assert-True ([string]::IsNullOrWhiteSpace($state.previousCommit)) `
    "rollback must consume previousCommit to prevent ping-pong"
Assert-True ($state.lastAction -eq "rollback") "rollback lastAction mismatch"
$secondRollback = Invoke-Update @("-Rollback", "-NoRestart")
Assert-True ($secondRollback.ExitCode -eq 2) `
    "second rollback must fail closed instead of ping-pong"

# Deterministically exercise the post-pin ledger failure paths. These hooks are
# inert unless CI=true and a non-default custom StatePath is in use.
$env:CI = "true"
$env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE = "1"
$restoredFailure = Invoke-Update @(
    "-TargetCommit", $commitD, "-NoRestart"
)
Assert-True ($restoredFailure.ExitCode -eq 2) `
    "ledger failure with successful source restoration must exit 2"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitB) `
    "ledger failure did not restore the pre-deploy commit"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitB) `
    "failed ledger write mutated durable state"

$env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE = "1"
$restoreFailure = Invoke-Update @(
    "-TargetCommit", $commitD, "-NoRestart"
)
Assert-True ($restoreFailure.ExitCode -eq 4) `
    "ledger plus source-restore failure must exit 4"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitD) `
    "restore-failure fixture did not preserve the landed target state"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitB) `
    "restore-failure fixture unexpectedly rewrote the ledger"
Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE
Remove-Item Env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE
Invoke-Git $deploy @("checkout", "--detach", $commitB) | Out-Null

# A pinned B behind newer main D is expected, not drift.
$driftHealthy = Invoke-Drift
Assert-True ($driftHealthy.ExitCode -eq 0) `
    "moving main behind-state must not trigger drift"

Invoke-Git $deploy @("checkout", "--detach", $commitC) | Out-Null
$headDrift = Invoke-Drift
Assert-True ($headDrift.ExitCode -eq 2) "HEAD mismatch must trigger drift"
Invoke-Git $deploy @("checkout", "--detach", $commitB) | Out-Null

$validState = Read-DeploymentState -StatePath $statePath
[IO.File]::WriteAllText($statePath, "{malformed", (New-Object Text.UTF8Encoding($false)))
$malformed = Invoke-Drift
Assert-True ($malformed.ExitCode -eq 2) "malformed ledger must trigger drift"
Write-DeploymentStateAtomic -StatePath $statePath -State $validState

$insecureAcl = Get-Acl -LiteralPath $statePath
$insecureAcl.SetAccessRuleProtection($false, $true)
Set-Acl -LiteralPath $statePath -AclObject $insecureAcl
$insecure = Invoke-Drift
Assert-True ($insecure.ExitCode -eq 2) "insecure ledger ACL must trigger drift"
Set-Acl -LiteralPath $statePath -AclObject (New-DeploymentStateAcl)
Assert-DeploymentStateAcl -Path $statePath

# Required tasks do not exist on the CI fixture host: the source pin must land,
# ledger result must say restart-failed, and exit 3 must distinguish this state.
$restartFailure = Invoke-Update @("-TargetCommit", $commitD)
Assert-True ($restartFailure.ExitCode -eq 3) `
    "missing required tasks must return pin-landed/restart-failed exit 3"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitD) "restart failure lost source pin"
Assert-True ($state.previousCommit -eq $commitB) `
    "restart failure lost bounded rollback target"
Assert-True ($state.lastResult -eq "restart-failed") `
    "restart failure ledger result mismatch"

$postFailureRollback = Invoke-Update @(
    "-Rollback", "-NoRestart"
)
Assert-True ($postFailureRollback.ExitCode -eq 0) `
    "rollback after restart failure failed"

Write-Host "immutable deployment ledger behavior contract: PASS"
