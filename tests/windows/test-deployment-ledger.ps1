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
$script:ControllerAuthorityCommit = ""
$script:FailClosedTaskNames = @(
    "platform-ai-ci-ledger-live-stt",
    "platform-ai-ci-ledger-meeting-ai"
)

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
    $command = (("& '{0}' {1}{2}" -f `
        $Script.Replace("'", "''"), ($commandTokens -join " "), $confirmToken) +
        [Environment]::NewLine + "exit `$LASTEXITCODE")
    $invocationId = [Guid]::NewGuid().ToString("N")
    $wrapperPath = Join-Path $tempRoot ("deploy-child-{0}.ps1" -f $invocationId)
    $stdoutPath = Join-Path $tempRoot ("deploy-child-{0}.stdout" -f $invocationId)
    $stderrPath = Join-Path $tempRoot ("deploy-child-{0}.stderr" -f $invocationId)
    try {
        [IO.File]::WriteAllText(
            $wrapperPath,
            $command + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false))
        )
        $process = Start-Process powershell.exe -NoNewWindow -Wait -PassThru `
            -ArgumentList @(
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", ('"{0}"' -f $wrapperPath)
            ) `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $output = @()
        if (Test-Path -LiteralPath $stdoutPath) {
            $output += @(Get-Content -LiteralPath $stdoutPath)
        }
        if (Test-Path -LiteralPath $stderrPath) {
            $output += @(Get-Content -LiteralPath $stderrPath)
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Output = $output
        }
    } finally {
        foreach ($path in @($wrapperPath, $stdoutPath, $stderrPath)) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-Update {
    param([string[]]$ExtraArgs)
    $controllerCommit = ""
    $controllerIndex = [Array]::IndexOf(
        $ExtraArgs,
        "-ControllerCommit"
    )
    if ($controllerIndex -ge 0 -and
        $controllerIndex + 1 -lt $ExtraArgs.Count -and
        $ExtraArgs[$controllerIndex + 1] -match '^[0-9a-f]{40}$') {
        $controllerCommit = $ExtraArgs[$controllerIndex + 1]
    } elseif (-not [string]::IsNullOrWhiteSpace(
        $script:ControllerAuthorityCommit
    )) {
        $controllerCommit = $script:ControllerAuthorityCommit
        $ExtraArgs += @("-ControllerCommit", $controllerCommit)
    } elseif ($ExtraArgs -contains "-Rollback") {
        $controllerCommit = "$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim()
    } else {
        $targetIndex = [Array]::IndexOf($ExtraArgs, "-TargetCommit")
        if ($targetIndex -ge 0 -and $targetIndex + 1 -lt $ExtraArgs.Count -and
            $ExtraArgs[$targetIndex + 1] -match '^[0-9a-f]{40}$') {
            $controllerCommit = $ExtraArgs[$targetIndex + 1]
        }
    }
    $arguments = @(
        "-RepoRoot", $deploy,
        "-StatePath", $statePath,
        "-Branch", "main"
    ) + $ExtraArgs
    if ([string]::IsNullOrWhiteSpace($controllerCommit)) {
        return Invoke-ChildPowerShell -Script $updateScript -ScriptArgs $arguments `
            -SuppressConfirmation
    }
    $controller = Join-Path $fixtureRoot (
        "controller-{0}" -f [Guid]::NewGuid().ToString("N")
    )
    try {
        Invoke-Git $source @("worktree", "add", "--detach", $controller, $controllerCommit) |
            Out-Null
        $targetUpdate = Join-Path $controller "deploy\gpu-host\update.ps1"
        Assert-True (Test-Path -LiteralPath $targetUpdate -PathType Leaf) `
            "exact-target control worktree is missing update.ps1"
        return Invoke-ChildPowerShell -Script $targetUpdate -ScriptArgs $arguments `
            -SuppressConfirmation
    } finally {
        if (Test-Path -LiteralPath $controller) {
            Invoke-Git $source @("worktree", "remove", "--force", $controller) |
                Out-Null
        }
    }
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

function Set-TestScheduledTaskEnabled {
    param([string]$TaskName, [bool]$Enabled)
    $mode = if ($Enabled) { "/Enable" } else { "/Disable" }
    & schtasks.exe /Change /TN $TaskName $mode *> $null
    Assert-True ($LASTEXITCODE -eq 0) `
        "failed to change test task enabled state: $TaskName"
}

function Test-TestScheduledTaskEnabled {
    param([string]$TaskName)
    $xmlText = @(& schtasks.exe /Query /TN $TaskName /XML 2> $null)
    if ($LASTEXITCODE -ne 0 -or $xmlText.Count -eq 0) { return $false }
    [xml]$taskXml = $xmlText -join [Environment]::NewLine
    $enabledNodes = @($taskXml.Task.Settings.ChildNodes | Where-Object {
        $_.LocalName -eq "Enabled"
    })
    if ($enabledNodes.Count -eq 0) { return $true }
    if ($enabledNodes.Count -ne 1) { return $false }
    return [Convert]::ToBoolean([string]$enabledNodes[0].InnerText)
}

function Initialize-TestScheduledTasks {
    foreach ($taskName in $script:FailClosedTaskNames) {
        & schtasks.exe /Create /TN $taskName `
            /TR "cmd.exe /c exit 0" /SC DAILY /ST 23:59 `
            /RU SYSTEM /RL HIGHEST /F *> $null
        Assert-True ($LASTEXITCODE -eq 0) `
            "failed to create fail-closed test task: $taskName"
        Assert-True (Test-TestScheduledTaskEnabled -TaskName $taskName) `
            "fail-closed test task was not enabled: $taskName"
    }
}

function Assert-TestScheduledTasksEnabled {
    param([bool]$ExpectedEnabled)
    foreach ($taskName in $script:FailClosedTaskNames) {
        Assert-True (
            (Test-TestScheduledTaskEnabled -TaskName $taskName) -eq
                $ExpectedEnabled
        ) "unexpected enabled state for test task: $taskName"
    }
}

function Assert-TestFailClosedPortsUnused {
    $lines = @(& netstat.exe -ano -p TCP 2> $null)
    Assert-True ($LASTEXITCODE -eq 0) "netstat failed in fail-closed assertion"
    foreach ($port in @(61234, 61235)) {
        $pattern = '^\s*TCP\s+\S+:' + $port + '\s+\S+\s+LISTENING\s+\d+\s*$'
        Assert-True (@($lines | Where-Object { [string]$_ -match $pattern }).Count -eq 0) `
            "fail-closed test port still has a listener: $port"
    }
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
New-Item -ItemType Directory -Path (Join-Path $source "deploy") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "deploy\gpu-host") `
    -Destination (Join-Path $source "deploy\gpu-host") -Recurse -Force
Invoke-Git $source @("add", "deploy/gpu-host") | Out-Null
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

# Model an approved old deployment target that does not contain the recovery
# updater. The later controller commit restores the current exact updater and
# remains the authority for recovery and its subsequent bounded rollback.
[IO.File]::WriteAllText(
    (Join-Path $source "deploy\gpu-host\update.ps1"),
    "# legacy target intentionally lacks the recovery controller contract",
    (New-Object Text.UTF8Encoding($false))
)
[IO.File]::WriteAllText(
    (Join-Path $source "d.txt"),
    "D",
    (New-Object Text.UTF8Encoding($false))
)
Invoke-Git $source @("add", "deploy/gpu-host/update.ps1", "d.txt") | Out-Null
Invoke-Git $source @("commit", "-m", "fixture legacy deployment target") | Out-Null
Invoke-Git $source @("push", "origin", "main") | Out-Null
$commitD = "$(Invoke-Git $source @('rev-parse', 'HEAD'))".Trim().ToLowerInvariant()
Copy-Item -LiteralPath $updateScript `
    -Destination (Join-Path $source "deploy\gpu-host\update.ps1") -Force
Invoke-Git $source @("add", "deploy/gpu-host/update.ps1") | Out-Null
$recoveryControllerCommit = New-SourceCommit `
    -Name "controller-authority.txt" -Content "controller-authority"
$script:ControllerAuthorityCommit = $recoveryControllerCommit
Initialize-TestScheduledTasks
$env:PLATFORM_AI_TEST_FAIL_CLOSED_TASK_NAMES = `
    $script:FailClosedTaskNames -join ","
$env:PLATFORM_AI_TEST_FAIL_CLOSED_PORTS = "61234,61235"
$savedGithubActions = $env:GITHUB_ACTIONS
$savedRunnerEnvironment = $env:RUNNER_ENVIRONMENT
try {
    $env:CI = "true"
    $env:GITHUB_ACTIONS = "false"
    $env:RUNNER_ENVIRONMENT = "self-hosted"
    $env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE = "1"
    $inertFault = Invoke-Update @("-TargetCommit", $commitC, "-NoRestart")
    Assert-True ($inertFault.ExitCode -eq 0) `
        "CI=true alone must not activate deployment fault injection"
} finally {
    Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE `
        -ErrorAction SilentlyContinue
    if ($null -eq $savedGithubActions) { Remove-Item Env:GITHUB_ACTIONS }
    else { $env:GITHUB_ACTIONS = $savedGithubActions }
    if ($null -eq $savedRunnerEnvironment) { Remove-Item Env:RUNNER_ENVIRONMENT }
    else { $env:RUNNER_ENVIRONMENT = $savedRunnerEnvironment }
}
$whatIf = Invoke-Update @("-TargetCommit", $commitD, "-NoRestart", "-WhatIf")
Assert-True ($whatIf.ExitCode -eq 0) "WhatIf validation failed"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) "WhatIf mutated ledger"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
    "WhatIf mutated HEAD"

# Out-of-band source drift must remain fail-closed unless the operator uses the
# explicit recovery mode from an exact-target control checkout. Recovery keeps
# the trusted ledger currentCommit as rollback anchor; it never adopts drift.
$preRecoveryState = Read-DeploymentState -StatePath $statePath
Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$blockedDriftDeploy = Invoke-Update @("-TargetCommit", $commitD, "-NoRestart")
Assert-True ($blockedDriftDeploy.ExitCode -eq 2) `
    "normal deploy must reject HEAD/ledger drift"
$blockedNoRestartRecovery = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit, "-NoRestart"
)
Assert-True ($blockedNoRestartRecovery.ExitCode -eq 2) `
    "ledger drift recovery must not bypass restart acceptance"
$reconcileWhatIf = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit, "-WhatIf"
)
Assert-True ($reconcileWhatIf.ExitCode -eq 0) `
    "ledger drift recovery WhatIf validation failed"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitA) `
    "ledger drift recovery WhatIf mutated HEAD"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) `
    "ledger drift recovery WhatIf mutated ledger"

# A checkout that lands the target before reset or pin-postcondition failure is
# already a source mutation. Both paths must restore trusted C/B and reaccept C;
# a direct exit would leave enabled tasks able to load unaccepted target files.
foreach ($pinFault in @(
    "PLATFORM_AI_TEST_INJECT_PIN_RESET_FAILURE",
    "PLATFORM_AI_TEST_INJECT_PIN_POSTCONDITION_FAILURE"
)) {
    Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
    [Environment]::SetEnvironmentVariable($pinFault, "1", "Process")
    $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "accept"
    $pinFailure = Invoke-Update @(
        "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
        "-ControllerCommit", $recoveryControllerCommit
    )
    Assert-True ($pinFailure.ExitCode -eq 4) `
        "partial pin failure must retain mutation-failed exit 4: $pinFault"
    Assert-True (($pinFailure.Output -join " | ") -match
        "trusted source/ledger restored and runtime reaccepted") `
        "partial pin failure did not reaccept trusted runtime: $pinFault"
    $state = Read-DeploymentState -StatePath $statePath
    Assert-True ($state.currentCommit -eq $commitC -and
        $state.previousCommit -eq $commitB) `
        "partial pin failure lost the trusted C/B pair: $pinFault"
    Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
        "partial pin failure did not restore trusted source: $pinFault"
    Assert-TestScheduledTasksEnabled -ExpectedEnabled $true
    [Environment]::SetEnvironmentVariable($pinFault, $null, "Process")
    Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE
}
Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null

# A ledger-write failure after reconciliation pinning must restore the trusted
# ledger currentCommit, not the observed drift commit, and must reaccept that
# trusted runtime before returning the ledger failure.
$env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE = "1"
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "accept"
$reconcileLedgerFailure = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($reconcileLedgerFailure.ExitCode -eq 2) `
    "reconciliation ledger failure with trusted restore must exit 2"
Assert-True (($reconcileLedgerFailure.Output -join " | ") -match
    "trusted source restored and runtime reaccepted") `
    "reconciliation ledger failure did not reaccept the trusted runtime"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC -and
    $state.previousCommit -eq $commitB) `
    "reconciliation ledger failure mutated the trusted ledger pair"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
    "reconciliation ledger failure restored the observed drift commit"
Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

# A failure after the atomic writer has already replaced the ledger must still
# restore and read back the exact trusted C/B state before runtime acceptance.
Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$env:PLATFORM_AI_TEST_INJECT_LEDGER_POST_WRITE_FAILURE = "1"
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "accept"
$postWriteLedgerFailure = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($postWriteLedgerFailure.ExitCode -eq 2) `
    "post-write reconciliation ledger failure must exit 2"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC -and
    $state.previousCommit -eq $commitB) `
    "post-write ledger failure did not restore the exact trusted ledger pair"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
    "post-write ledger failure did not restore the trusted source"
Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_POST_WRITE_FAILURE
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

# An unexpected acceptance exception after source/ledger restoration must
# persistently disable both tasks instead of escaping the fail-closed path.
Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE = "1"
$env:PLATFORM_AI_TEST_INJECT_ACCEPTANCE_EXCEPTION = "1"
$acceptanceException = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($acceptanceException.ExitCode -eq 4) `
    "reconciliation acceptance exception must exit 4"
Assert-TestScheduledTasksEnabled -ExpectedEnabled $false
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC -and
    $state.previousCommit -eq $commitB) `
    "acceptance exception changed the trusted ledger pair"
Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE
Remove-Item Env:PLATFORM_AI_TEST_INJECT_ACCEPTANCE_EXCEPTION
foreach ($taskName in $script:FailClosedTaskNames) {
    Set-TestScheduledTaskEnabled -TaskName $taskName -Enabled $true
}

# Source restoration failure is also a persistent runtime fence, even though
# the target source remains landed and the trusted ledger remains C/B.
Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE = "1"
$env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE = "1"
$sourceRestoreFailure = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($sourceRestoreFailure.ExitCode -eq 4) `
    "reconciliation source-restore failure must exit 4"
Assert-TestScheduledTasksEnabled -ExpectedEnabled $false
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC -and
    $state.previousCommit -eq $commitB) `
    "source-restore failure changed the trusted ledger pair"
Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE
Remove-Item Env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE
foreach ($taskName in $script:FailClosedTaskNames) {
    Set-TestScheduledTaskEnabled -TaskName $taskName -Enabled $true
}

Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
# A rejected reconciliation target must restore the trusted ledger pair, never
# the observed drift commit.
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "reject-then-accept"
$rejectedReconcile = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($rejectedReconcile.ExitCode -eq 3) `
    "rejected reconciliation target must preserve target-failed exit 3"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) `
    "reconciliation rollback adopted the observed drift commit"
Assert-True ($state.previousCommit -eq $commitB) `
    "reconciliation rollback lost the trusted previous ledger anchor"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
    "reconciliation rollback did not restore trusted ledger currentCommit"
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "reject-twice"
$failedRollbackReconcile = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($failedRollbackReconcile.ExitCode -eq 4) `
    "reconciliation target plus rollback acceptance failure must exit 4"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) `
    "failed reconciliation rollback adopted the observed drift commit"
Assert-True ($state.previousCommit -eq $commitB) `
    "failed reconciliation rollback lost the trusted previous anchor"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
    "failed reconciliation rollback did not restore trusted currentCommit"
Assert-TestScheduledTasksEnabled -ExpectedEnabled $false
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$blockedFencedRecovery = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($blockedFencedRecovery.ExitCode -eq 2) `
    "fenced runtime recovery must require the explicit recovery switch"
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "accept"
$reconciled = Invoke-Update @(
    "-TargetCommit", $commitD, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit,
    "-RecoverFencedRuntime"
)
Assert-True ($reconciled.ExitCode -eq 0) (
    "ledger drift recovery failed: exit={0}; output={1}" -f `
    $reconciled.ExitCode, ($reconciled.Output -join " | ")
)
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitD) `
    "ledger drift recovery currentCommit mismatch"
Assert-True ($state.previousCommit -eq $commitC) `
    "ledger drift recovery adopted drift instead of trusted ledger anchor"
Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitD) `
    "ledger drift recovery HEAD mismatch"
Assert-TestScheduledTasksEnabled -ExpectedEnabled $true
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

# The same independent controller authority must remain usable for the bounded
# rollback immediately after an old target has been recovered. Do not use
# -NoRestart here: exercise the rollback acceptance path under the injected
# deterministic acceptance contract.
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "accept"
$postRecoveryRollback = Invoke-Update @(
    "-Rollback", "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($postRecoveryRollback.ExitCode -eq 0) `
    "independent controller could not roll back the recovered old target"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) `
    "post-recovery rollback did not restore the trusted anchor"
Assert-True ([string]::IsNullOrWhiteSpace($state.previousCommit)) `
    "post-recovery rollback did not consume its bounded slot"
Assert-True ($state.lastResult -eq "tasks-restarted") `
    "post-recovery rollback did not exercise restart acceptance"
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

# Returning drift directly to the already-trusted current commit must preserve
# the existing previousCommit instead of writing current=current.
Write-DeploymentStateAtomic -StatePath $statePath -State $preRecoveryState
Invoke-Git $deploy @("checkout", "--detach", $commitA) | Out-Null
$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "accept"
$sameTargetRecovery = Invoke-Update @(
    "-TargetCommit", $commitC, "-ReconcileLedgerDrift",
    "-ControllerCommit", $recoveryControllerCommit
)
Assert-True ($sameTargetRecovery.ExitCode -eq 0) `
    "same-target ledger drift recovery failed"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC -and
    $state.previousCommit -eq $commitB) `
    "same-target recovery lost the existing bounded rollback anchor"
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

$short = Invoke-Update @("-TargetCommit", $commitD.Substring(0, 12), `
    "-NoRestart")
Assert-True ($short.ExitCode -eq 2) (
    "short commit must fail with guard exit 2: actual={0}; output={1}" -f `
    $short.ExitCode, ($short.Output -join " | ")
)

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
# inert outside a GitHub-hosted runner and a StatePath rooted under RUNNER_TEMP.
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
Assert-TestScheduledTasksEnabled -ExpectedEnabled $false
Remove-Item Env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE
Remove-Item Env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE
foreach ($taskName in $script:FailClosedTaskNames) {
    Set-TestScheduledTaskEnabled -TaskName $taskName -Enabled $true
}
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

# Fault injection rejects both target and rollback acceptance. Source must still
# restore and the ledger must distinguish rollback-failed from target rejection.
$anchorDeploy = Invoke-Update @("-TargetCommit", $commitC, "-NoRestart")
Assert-True ($anchorDeploy.ExitCode -eq 0) `
    "rollback-anchor fixture deploy failed"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC -and
    $state.previousCommit -eq $commitB) `
    "rollback-anchor fixture did not create current=C, previous=B"

# Result writes happen after source pinning. Exercise both the pre-write and
# post-replace failures at the target-rejected write (invocation 1) and the
# rollback-failed write (invocation 2). Every path must restore the trusted C/B
# pair and persistently fence tasks/listeners before exit 4.
foreach ($faultPoint in @("1-pre", "1-post", "2-pre", "2-post")) {
    $env:PLATFORM_AI_TEST_INJECT_RESULT_WRITE_FAILURE = $faultPoint
    if ($faultPoint.StartsWith("1-")) {
        $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "reject-then-accept"
    } else {
        $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "reject-twice"
    }
    $resultWriteFailure = Invoke-Update @("-TargetCommit", $commitD)
    Assert-True ($resultWriteFailure.ExitCode -eq 4) `
        "result-write failure must exit 4 at $faultPoint"
    $state = Read-DeploymentState -StatePath $statePath
    Assert-True ($state.currentCommit -eq $commitC -and
        $state.previousCommit -eq $commitB) `
        "result-write failure lost the trusted C/B pair at $faultPoint"
    Assert-True ("$(Invoke-Git $deploy @('rev-parse', 'HEAD'))".Trim() -eq $commitC) `
        "result-write failure did not restore trusted source at $faultPoint"
    Assert-TestScheduledTasksEnabled -ExpectedEnabled $false
    Assert-TestFailClosedPortsUnused
    Remove-Item Env:PLATFORM_AI_TEST_INJECT_RESULT_WRITE_FAILURE
    Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE
    foreach ($taskName in $script:FailClosedTaskNames) {
        Set-TestScheduledTaskEnabled -TaskName $taskName -Enabled $true
    }
}

$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "reject-twice"
$restartFailure = Invoke-Update @("-TargetCommit", $commitD)
Assert-True ($restartFailure.ExitCode -eq 4) `
    "target plus rollback acceptance failure must return exit 4"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) `
    "automatic rollback did not restore the previous source commit"
Assert-True ($state.previousCommit -eq $commitB) `
    "failed deploy must preserve the pre-existing valid rollback anchor"
Assert-True ($state.lastResult -eq `
    "automatic-rollback-failed-injected-acceptance-failure") `
    "rollback acceptance failure ledger result mismatch"
Assert-TestScheduledTasksEnabled -ExpectedEnabled $false
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

$env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE = "reject-then-accept"
$acceptedRollback = Invoke-Update @(
    "-TargetCommit", $commitD, "-RecoverFencedRuntime"
)
Assert-True ($acceptedRollback.ExitCode -eq 3) `
    "rejected target with reaccepted rollback must retain target-failed exit 3"
$state = Read-DeploymentState -StatePath $statePath
Assert-True ($state.currentCommit -eq $commitC) `
    "successful automatic rollback did not restore previous source"
Assert-True ($state.previousCommit -eq $commitB) `
    "successful automatic rollback lost the pre-existing valid rollback anchor"
Assert-True ($state.lastResult -eq "automatic-rollback-accepted") `
    "successful automatic rollback ledger result mismatch"
Assert-TestScheduledTasksEnabled -ExpectedEnabled $true
Remove-Item Env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE

foreach ($taskName in $script:FailClosedTaskNames) {
    & schtasks.exe /Delete /TN $taskName /F *> $null
}
Remove-Item Env:PLATFORM_AI_TEST_FAIL_CLOSED_TASK_NAMES `
    -ErrorAction SilentlyContinue
Remove-Item Env:PLATFORM_AI_TEST_FAIL_CLOSED_PORTS `
    -ErrorAction SilentlyContinue

Write-Host "immutable deployment ledger behavior contract: PASS"
