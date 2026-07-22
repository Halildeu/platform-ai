<#
.SYNOPSIS
  Immutable GPU-host deploy update. Pins the deploy clone to an explicit full
  commit, records a hardened deployment ledger, and restarts scheduled tasks.

.DESCRIPTION
  A moving origin/main head is discovery truth, not a deploy artifact. Normal
  deploy requires -TargetCommit with exactly 40 hexadecimal characters. The
  commit must exist and remain an ancestor of origin/main after a fresh fetch.
  The resulting working tree is detached at that exact commit.

  The deploy clone (e.g. C:\Users\denetimpc\platform-ai) is a READ-ONLY MIRROR
  of origin/main. Development must NEVER happen here - see drift-guard.ps1 and
  the 2026-06-21 incident where 13 unpushed diarization commits (#161/#164) sat
  local-only on this clone (single point of failure, no GitHub backup).

  A SYSTEM + Administrators-only deployment-state.json records the current and
  bounded previous commit. -Rollback may target only that previous commit and
  consumes the rollback slot so repeated rollback cannot ping-pong revisions.

  Exit codes: 0 success; 2 guard/no-mutation failure; 3 source pin landed but
  scheduled-task restart failed; 4 automatic source restoration or rollback
  mutation failed. Restart acceptance is fail-closed: the service must become
  ready and complete the pinned sample-WAV stream protocol through final text,
  eof_ack, and drained before the deployment is accepted.

.PARAMETER RepoRoot
  Deploy clone path. Required. This script must execute from a separate,
  exact-target control checkout so the first rollout uses the target updater.

.PARAMETER Branch
  Tracking branch. Default 'main'. The deploy clone tracks main only.

.PARAMETER StatePath
  Hardened deployment ledger path. Defaults to ProgramData.

.PARAMETER Rollback
  Roll back only to deployment-state.json previousCommit.

.PARAMETER NoRestart
  Pin and ledger the working tree but do not restart scheduled tasks.

.PARAMETER NoConfirm
  Suppress ShouldProcess confirmation for a non-interactive controller child.
  This is a normal script switch and is safe through Windows PowerShell 5.1
  powershell.exe -File argument binding.

.EXAMPLE
  cd C:\platform-ai-control
  Set-ExecutionPolicy -Scope Process Bypass
  .\deploy\gpu-host\update.ps1 -RepoRoot C:\platform-ai `
    -TargetCommit <full-40-hex-commit>
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
  [string]$RepoRoot = "",
  [string]$Branch = "main",
  [string]$TargetCommit = "",
  [string]$StatePath = "C:\ProgramData\Acik\platform-ai\deployment-state.json",
  [switch]$Rollback,
  [switch]$NoRestart,
  [switch]$NoConfirm
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
if ($NoConfirm) { $ConfirmPreference = "None" }
$script:DeployExitGuard = 2
$script:DeployExitRestartFailed = 3
$script:DeployExitRollbackFailed = 4
$script:DeployMutex = $null
$script:DeployLockTaken = $false
$script:TestAcceptanceInvocation = 0
$script:DefaultDeploymentStatePath = `
  "C:\ProgramData\Acik\platform-ai\deployment-state.json"
$script:LegacyRollbackCompatCommit = `
  "512e9cc0fe4368d3cc91759dcd48756e54c2ad63"
$script:ResolvedRunnerTemp = ""
if (-not [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  try { $script:ResolvedRunnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP) }
  catch { $script:ResolvedRunnerTemp = "" }
}
$script:ResolvedStatePath = ""
try { $script:ResolvedStatePath = [IO.Path]::GetFullPath($StatePath) } catch { }
$script:TestFaultsEnabled = $false

function Test-GpuHostPathUnderRoot {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Root
  )

  try {
    $resolvedPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    return $resolvedPath.StartsWith(
      $resolvedRoot + '\',
      [StringComparison]::OrdinalIgnoreCase
    )
  } catch {
    return $false
  }
}

function Stop-Deploy {
  param(
    [Parameter(Mandatory = $true)][string]$Message,
    [Parameter(Mandatory = $true)][int]$Code
  )
  # Write-Error becomes terminating under this script's EAP=Stop and would
  # collapse every explicit operational exit code to process exit 1.
  [Console]::Error.WriteLine("[update] ERROR: {0}" -f $Message)
  if ($script:DeployMutex) {
    if ($script:DeployLockTaken) {
      try { $script:DeployMutex.ReleaseMutex() } catch { }
    }
    $script:DeployMutex.Dispose()
    $script:DeployMutex = $null
  }
  exit $Code
}

function Invoke-GitCapture {
  param([Parameter(Mandatory = $true)][string[]]$GitArgs)
  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $output = @(& git @GitArgs 2> $null)
    return [pscustomobject]@{
      ExitCode = $LASTEXITCODE
      Output = $output
    }
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

function Invoke-GitStream {
  param([Parameter(Mandatory = $true)][string[]]$GitArgs)
  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & git @GitArgs | Out-Host
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
  Stop-Deploy "RepoRoot is required; run this updater from an exact-target control checkout." `
    $script:DeployExitGuard
}
$controllerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$currentWindowsIdentity = ""
try {
  $currentWindowsIdentity = `
    [Security.Principal.WindowsIdentity]::GetCurrent().Name
} catch { }
$script:TestFaultsEnabled = (
  $env:CI -eq "true" -and
  $env:GITHUB_ACTIONS -eq "true" -and
  $env:RUNNER_ENVIRONMENT -eq "github-hosted" -and
  $currentWindowsIdentity -match '\\runneradmin$' -and
  -not [string]::IsNullOrWhiteSpace($script:ResolvedRunnerTemp) -and
  -not [string]::IsNullOrWhiteSpace($script:ResolvedStatePath) -and
  (Test-GpuHostPathUnderRoot -Path $controllerRoot `
    -Root $script:ResolvedRunnerTemp) -and
  (Test-GpuHostPathUnderRoot -Path $RepoRoot `
    -Root $script:ResolvedRunnerTemp) -and
  (Test-GpuHostPathUnderRoot -Path $script:ResolvedStatePath `
    -Root $script:ResolvedRunnerTemp)
)
if ([IO.Path]::GetFullPath($controllerRoot).TrimEnd('\').Equals(
    [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\'),
    [StringComparison]::OrdinalIgnoreCase
  )) {
  Stop-Deploy "Updater must run from a separate exact-target control checkout." `
    $script:DeployExitGuard
}

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
  Stop-Deploy "RepoRoot '$RepoRoot' is not a git clone. Pass -RepoRoot explicitly." `
    $script:DeployExitGuard
}
if (-not (Test-Path (Join-Path $controllerRoot ".git"))) {
  Stop-Deploy "ControllerRoot is not a git checkout." $script:DeployExitGuard
}
$legacyRuntimeEnv = Join-Path $RepoRoot "deploy\gpu-host\env.local.ps1"
if (Test-Path -LiteralPath $legacyRuntimeEnv -PathType Leaf) {
  Stop-Deploy "Legacy plaintext env.local.ps1 exists. Migrate to DPAPI live-stt.env and securely remove it before deploy." `
    $script:DeployExitGuard
}
if ($Branch -notmatch '^[A-Za-z0-9._/-]+$') {
  Stop-Deploy "Branch contains unsupported characters." $script:DeployExitGuard
}

$stateModule = Join-Path $PSScriptRoot "deployment-state.ps1"
if (-not (Test-Path -LiteralPath $stateModule -PathType Leaf)) {
  Stop-Deploy "Missing deployment-state.ps1 beside update.ps1." `
    $script:DeployExitGuard
}
. $stateModule

$script:DeployMutex = New-Object Threading.Mutex(
  $false,
  "Global\platform-ai-gpu-deploy-v1"
)
try {
  $script:DeployLockTaken = $script:DeployMutex.WaitOne(0)
} catch [Threading.AbandonedMutexException] {
  $script:DeployLockTaken = $true
}
if (-not $script:DeployLockTaken) {
  Stop-Deploy "Another GPU deploy operation is already active." `
    $script:DeployExitGuard
}

Set-Location $RepoRoot
Write-Host "[update] repo=$RepoRoot branch=$Branch rollback=$Rollback" `
  -ForegroundColor Cyan

# Refresh remote refs before every object or ancestry decision.
if ((Invoke-GitStream -GitArgs @("fetch", "--prune", "origin")) -ne 0) {
  Stop-Deploy "git fetch failed (network/auth). No mutation." `
    $script:DeployExitGuard
}

$originRef = "origin/{0}" -f $Branch
$originRemoteRef = "refs/remotes/{0}" -f $originRef
$unpushedRange = "{0}..HEAD" -f $originRef
$branchRef = "refs/remotes/{0}" -f $originRef

$originCheck = Invoke-GitCapture -GitArgs @(
  "rev-parse", "--verify", "--quiet", $originRemoteRef
)
if ($originCheck.ExitCode -ne 0) {
  Stop-Deploy "$originRef not found after fetch. No mutation." `
    $script:DeployExitGuard
}

$headResult = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
if ($headResult.ExitCode -ne 0 -or $headResult.Output.Count -ne 1) {
  Stop-Deploy "git rev-parse HEAD failed. No mutation." `
    $script:DeployExitGuard
}
$before = "$($headResult.Output[0])".Trim().ToLowerInvariant()
if ($before -notmatch '^[0-9a-f]{40}$') {
  Stop-Deploy "Current HEAD is not a full commit id. No mutation." `
    $script:DeployExitGuard
}

$dirtyResult = Invoke-GitCapture -GitArgs @(
  "status", "--porcelain", "--untracked-files=no"
)
if ($dirtyResult.ExitCode -ne 0) {
  Stop-Deploy "git status failed. No mutation." $script:DeployExitGuard
}

$unpushedResult = Invoke-GitCapture -GitArgs @("rev-list", $unpushedRange)
if ($unpushedResult.ExitCode -ne 0) {
  Stop-Deploy "git rev-list $unpushedRange failed. No mutation." `
    $script:DeployExitGuard
}

if ($dirtyResult.Output.Count -gt 0) {
  Stop-Deploy "Dirty tracked tree detected. Preserve work before deploy." `
    $script:DeployExitGuard
}
if ($unpushedResult.Output.Count -gt 0) {
  Stop-Deploy "Local commit(s) not present in $originRef. Preserve via push + PR." `
    $script:DeployExitGuard
}

$state = $null
try {
  $state = Read-DeploymentState -StatePath $StatePath
} catch {
  Stop-Deploy ("Deployment ledger rejected: {0}" -f $_.Exception.Message) `
    $script:DeployExitGuard
}
if ($state -and $state.currentCommit -ne $before) {
  Stop-Deploy "HEAD does not match deployment ledger currentCommit. No mutation." `
    $script:DeployExitGuard
}

$action = "deploy"
$target = ""
$previous = $null
if ($Rollback) {
  $action = "rollback"
  if (-not [string]::IsNullOrWhiteSpace($TargetCommit)) {
    Stop-Deploy "-Rollback cannot be combined with -TargetCommit." `
      $script:DeployExitGuard
  }
  if (-not $state -or [string]::IsNullOrWhiteSpace($state.previousCommit)) {
    Stop-Deploy "Rollback requested but ledger has no previousCommit." `
      $script:DeployExitGuard
  }
  $target = $state.previousCommit
} else {
  if ($TargetCommit.ToLowerInvariant() -notmatch '^[0-9a-f]{40}$') {
    Stop-Deploy "Normal deploy requires -TargetCommit with exactly 40 hex characters." `
      $script:DeployExitGuard
  }
  $target = $TargetCommit.ToLowerInvariant()
  if ($state) {
    if ($target -eq $before) { $previous = $state.previousCommit }
    else { $previous = $before }
  }
}

$objectSpec = $target + "^{commit}"
$objectResult = Invoke-GitCapture -GitArgs @("rev-parse", "--verify", $objectSpec)
if ($objectResult.ExitCode -ne 0 -or $objectResult.Output.Count -ne 1 -or
    "$($objectResult.Output[0])".Trim().ToLowerInvariant() -ne $target) {
  Stop-Deploy "Target is not an available exact commit object. No mutation." `
    $script:DeployExitGuard
}
$ancestorResult = Invoke-GitCapture -GitArgs @(
  "merge-base", "--is-ancestor", $target, $originRef
)
if ($ancestorResult.ExitCode -ne 0) {
  Stop-Deploy "Target is not an ancestor of $originRef. No mutation." `
    $script:DeployExitGuard
}

$controllerHead = Invoke-GitCapture -GitArgs @(
  "-C", $controllerRoot, "rev-parse", "HEAD"
)
$expectedControllerCommit = if ($Rollback) { $before } else { $target }
if ($controllerHead.ExitCode -ne 0 -or $controllerHead.Output.Count -ne 1 -or
    "$($controllerHead.Output[0])".Trim().ToLowerInvariant() -ne $expectedControllerCommit) {
  Stop-Deploy "Control checkout HEAD must equal the approved controller commit." `
    $script:DeployExitGuard
}
$controllerDirty = Invoke-GitCapture -GitArgs @(
  "-C", $controllerRoot, "status", "--porcelain", "--untracked-files=all"
)
if ($controllerDirty.ExitCode -ne 0 -or $controllerDirty.Output.Count -gt 0) {
  Stop-Deploy "Control checkout must be clean, including untracked files." `
    $script:DeployExitGuard
}
$deployOrigin = Invoke-GitCapture -GitArgs @("remote", "get-url", "origin")
$controllerOrigin = Invoke-GitCapture -GitArgs @(
  "-C", $controllerRoot, "remote", "get-url", "origin"
)
if ($deployOrigin.ExitCode -ne 0 -or $controllerOrigin.ExitCode -ne 0 -or
    $deployOrigin.Output.Count -ne 1 -or $controllerOrigin.Output.Count -ne 1 -or
    -not "$($deployOrigin.Output[0])".Trim().Equals(
      "$($controllerOrigin.Output[0])".Trim(),
      [StringComparison]::OrdinalIgnoreCase
    )) {
  Stop-Deploy "Deploy and control checkouts must use the same origin." `
    $script:DeployExitGuard
}

if (-not $PSCmdlet.ShouldProcess(
    $RepoRoot,
    ("{0} immutable commit {1}" -f $action, $target)
  )) {
  Write-Host "[update] WhatIf/declined: validation passed; no mutation." `
    -ForegroundColor Yellow
  $script:DeployMutex.ReleaseMutex()
  $script:DeployMutex.Dispose()
  $script:DeployMutex = $null
  exit 0
}

$pinFailedCode = $script:DeployExitRollbackFailed
if ((Invoke-GitStream -GitArgs @("checkout", "--detach", $target)) -ne 0 -or
    (Invoke-GitStream -GitArgs @("reset", "--hard", $target)) -ne 0) {
  Stop-Deploy "Failed to pin target commit $target." $pinFailedCode
}
$afterResult = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
$symbolicResult = Invoke-GitCapture -GitArgs @("symbolic-ref", "-q", "HEAD")
if ($afterResult.ExitCode -ne 0 -or $afterResult.Output.Count -ne 1 -or
    "$($afterResult.Output[0])".Trim().ToLowerInvariant() -ne $target -or
    $symbolicResult.ExitCode -eq 0) {
  Stop-Deploy "Detached exact-pin postcondition failed." $pinFailedCode
}

$script:DeploymentLedgerRecord = New-DeploymentStateRecord -CurrentCommit $target `
  -PreviousCommit $previous -BranchRef $branchRef -Action $action `
  -Result "source-pinned"
try {
  if ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE -eq "1") {
    throw "CI fault injection: deployment ledger write failure"
  }
  Write-DeploymentStateAtomic -StatePath $StatePath -State $script:DeploymentLedgerRecord
} catch {
  Write-Host "[update] ledger write failed; restoring pre-deploy commit" `
    -ForegroundColor Red
  $restoreOk = $false
  if (-not ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE -eq "1")) {
    $restoreOk = ((Invoke-GitStream -GitArgs @(
      "checkout", "--detach", $before
    )) -eq 0)
  }
  if ($restoreOk) {
    $restoreOk = ((Invoke-GitStream -GitArgs @(
      "reset", "--hard", $before
    )) -eq 0)
  }
  if ($restoreOk) {
    $restoreHead = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
    $restoreSymbolic = Invoke-GitCapture -GitArgs @(
      "symbolic-ref", "-q", "HEAD"
    )
    $restoreOk = ($restoreHead.ExitCode -eq 0 -and
      $restoreHead.Output.Count -eq 1 -and
      "$($restoreHead.Output[0])".Trim().ToLowerInvariant() -eq $before -and
      $restoreSymbolic.ExitCode -ne 0)
  }
  if (-not $restoreOk) {
    Stop-Deploy "Ledger write and automatic source restoration failed." `
      $script:DeployExitRollbackFailed
  }
  Stop-Deploy ("Ledger write failed; source restored: {0}" -f `
    $_.Exception.Message) $script:DeployExitGuard
}
Write-Host "[update] $before -> $target (detached immutable pin)" `
  -ForegroundColor Green

function Set-DeploymentLedgerResult {
  param([Parameter(Mandatory = $true)][string]$Result)

  $script:DeploymentLedgerRecord["lastResult"] = $Result
  $script:DeploymentLedgerRecord["timestampUtc"] = [DateTime]::UtcNow.ToString("o")
  try {
    Write-DeploymentStateAtomic -StatePath $StatePath `
      -State $script:DeploymentLedgerRecord
  } catch {
    Stop-Deploy ("Pinned source but ledger result update failed: {0}" -f `
      $_.Exception.Message) $script:DeployExitRollbackFailed
  }
}

$runtimeContract = Join-Path $controllerRoot "deploy\gpu-host\live-stt-runtime-contract.ps1"
$taskActionContract = Join-Path $controllerRoot "deploy\gpu-host\task-action-contract.ps1"
$restartAcceptance = Join-Path $controllerRoot "deploy\gpu-host\restart-acceptance.ps1"
if (-not $NoRestart) {
  $testAcceptanceInjected = (
    $script:TestFaultsEnabled -and
    $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE -in @(
      "reject-twice", "reject-then-accept"
    )
  )
  if (-not $testAcceptanceInjected -and (
      -not (Test-Path -LiteralPath $runtimeContract -PathType Leaf) -or
      -not (Test-Path -LiteralPath $taskActionContract -PathType Leaf) -or
      -not (Test-Path -LiteralPath $restartAcceptance -PathType Leaf))) {
    Set-DeploymentLedgerResult -Result "restart-failed"
    Stop-Deploy "Pinned source is missing a GPU-host runtime/action contract." `
      $script:DeployExitRestartFailed
  }
  if (-not $testAcceptanceInjected) {
    . $runtimeContract
    . $taskActionContract
    . $restartAcceptance
  }
}

# 4. Restart the deploy scheduled tasks so they pick up the new code. Use the
#    always-present schtasks.exe rather than the *-ScheduledTask cmdlets: the
#    ScheduledTasks module is ABSENT on some hosts (this GPU host's Windows
#    PowerShell 5.1 has no Restart-ScheduledTask - Get-ScheduledTask would throw
#    CommandNotFound and, under $ErrorActionPreference=Stop, abort the whole
#    update after the git pin already landed). #193 follow-up; Codex review #194.
#
#    CRITICAL: schtasks writes benign stderr on /Query-missing and /End-not-
#    running. PowerShell's `2>&1` pipe can wrap native stderr into a
#    NativeCommandError that, under $ErrorActionPreference=Stop, terminates BEFORE
#    the $LASTEXITCODE check - re-introducing the same "git pin landed, restart
#    aborted" failure. So route every call through a helper that drops stderr
#    WITHOUT the PS 2>&1 pipe and forces EAP=Continue around the native call.
# Explicit -Action/-TaskName (NOT ValueFromRemainingArguments, which is unreliable
# on Windows PowerShell 5.1 - it can collapse the remaining positionals into one
# argument, mangling `/Query /TN <task>` so a present task reads as "not installed"
# and the restart is silently skipped). Codex review #194.
function Invoke-SchtasksTask {
  param(
    [Parameter(Mandatory = $true)][ValidateSet("/Query", "/End", "/Run")][string]$Action,
    [Parameter(Mandatory = $true)][string]$TaskName
  )
  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & schtasks.exe $Action /TN $TaskName 1> $null 2> $null
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

function Get-SchtasksTaskXml {
  param([Parameter(Mandatory = $true)][string]$TaskName)

  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $output = @(& schtasks.exe /Query /TN $TaskName /XML 2> $null)
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

function Get-TaskRuntimeContract {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    try {
        $query = Get-SchtasksTaskXml -TaskName $TaskName
        if ($query.ExitCode -ne 0 -or $query.Output.Count -eq 0) { throw "query-failed" }
        return Get-GpuHostTaskXmlContract -TaskName $TaskName `
          -TaskXml ($query.Output -join [Environment]::NewLine)
    } catch {
        return [pscustomobject]@{
          Valid = $false
          PythonExe = ""
          RepoRoot = ""
          Reason = [string]$_.Exception.Message
        }
    }
}

function Invoke-LiveSttFixtureAcceptance {
  param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
    [Parameter(Mandatory = $true)][double]$DeadlineSec,
    [Parameter(Mandatory = $true)][ValidateSet(
      "sample-tr-cv17-001",
      "sample-tr-cv17-002"
    )][string]$FixtureBaseName,
    [string]$Url = "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1",
    [int]$ConnectTimeoutCapSec = 30
  )

  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $smoke = Join-Path $controllerRoot "services\live-stt-service\scripts\live_stream_smoke.py"
    $wav = Join-Path $controllerRoot `
      ("services\live-stt-service\tests\fixtures\{0}.wav" -f $FixtureBaseName)
    $referenceText = Join-Path $controllerRoot `
      ("services\live-stt-service\tests\fixtures\{0}.txt" -f $FixtureBaseName)
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $smoke -PathType Leaf) -or
        -not (Test-Path -LiteralPath $wav -PathType Leaf) -or
        -not (Test-Path -LiteralPath $referenceText -PathType Leaf)) {
      Write-Host "[update] direct stream acceptance interpreter/fixture missing" `
        -ForegroundColor Yellow
      return $false
    }
    $remainingSec = $DeadlineSec - $Clock.Elapsed.TotalSeconds
    if ($remainingSec -le 5) { return $false }
    $connectTimeoutSec = [Math]::Max(1, [Math]::Min(
      $ConnectTimeoutCapSec,
      [Math]::Floor($remainingSec / 3)
    ))
    $finalWaitSec = [Math]::Max(1, [Math]::Min(
      120,
      [Math]::Floor($remainingSec - $connectTimeoutSec - 2)
    ))
    $arguments = @(
      $smoke,
      "--url", $Url,
      "--wav", $wav,
      "--reference-text", $referenceText,
      "--timeout-sec", "$connectTimeoutSec",
      "--final-wait-sec", "$finalWaitSec",
      "--min-final-word-coverage", "0.8",
      "--min-partial-events", "1",
      "--min-final-events", "1",
      "--min-reference-token-coverage", "0.8",
      "--max-word-error-rate", "0.25",
      "--max-transcript-gap-ms", "6000"
    )
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $PythonExe
    $startInfo.Arguments = (($arguments | ForEach-Object {
      ConvertTo-GpuHostWindowsArgument -Value ([string]$_)
    }) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { return $false }
    $remainingMs = [Math]::Max(1, [int](
      ($DeadlineSec - $Clock.Elapsed.TotalSeconds) * 1000
    ))
    if (-not $process.WaitForExit($remainingMs)) {
      try { $process.Kill() } catch { }
      $process.Dispose()
      return $false
    }
    $output = $process.StandardOutput.ReadToEnd()
    $exitCode = $process.ExitCode
    $process.Dispose()
    if ($exitCode -ne 0 -or
        -not (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec)) {
      Write-Host "[update] direct stream inference acceptance failed" -ForegroundColor Yellow
      return $false
    }
    try {
      $summary = $output | ConvertFrom-Json -ErrorAction Stop
      $referenceName = [IO.Path]::GetFileName($referenceText)
      $referenceContent = [IO.File]::ReadAllText($referenceText).Trim()
      $sha256 = [Security.Cryptography.SHA256]::Create()
      try {
        $referenceNameHash = ([BitConverter]::ToString(
          $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($referenceName))
        ) -replace '-', '').ToLowerInvariant().Substring(0, 12)
        $referenceTextHash = ([BitConverter]::ToString(
          $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($referenceContent))
        ) -replace '-', '').ToLowerInvariant().Substring(0, 12)
      } finally {
        $sha256.Dispose()
      }
      return (
        $summary.schema -eq "platform-ai.live-stt.stream-smoke.v1" -and
        $summary.ok -eq $true -and
        $summary.reference.artifact_id_sha256_12 -eq $referenceNameHash -and
        $summary.reference.text_sha256_12 -eq $referenceTextHash -and
        [int]$summary.reference.words -ge 1 -and
        [int]$summary.events.partial_count -ge 1 -and
        [int]$summary.events.final_count -ge 1 -and
        [int]$summary.events.final_hallucination_count -eq 0 -and
        [int]$summary.events.error_count -eq 0 -and
        $null -ne $summary.events.max_transcript_gap_ms -and
        [int]$summary.events.max_transcript_gap_ms -ge 0 -and
        [int]$summary.events.max_transcript_gap_ms -le 6000 -and
        [int]$summary.coverage.reference_words -eq `
          [int]$summary.reference.words -and
        [int]$summary.coverage.final_words -ge 1 -and
        [double]$summary.coverage.final_word_coverage -ge 0.8 -and
        [double]$summary.coverage.reference_token_coverage -ge 0.8 -and
        [double]$summary.coverage.word_error_rate -ge 0.0 -and
        [double]$summary.coverage.word_error_rate -le 0.25 -and
        [int]$summary.quality_gate.min_partial_events -eq 1 -and
        [int]$summary.quality_gate.min_final_events -eq 1 -and
        [double]$summary.quality_gate.min_final_word_coverage -eq 0.8 -and
        [double]$summary.quality_gate.min_reference_token_coverage -eq 0.8 -and
        [double]$summary.quality_gate.max_word_error_rate -eq 0.25 -and
        [int]$summary.quality_gate.max_transcript_gap_ms -eq 6000 -and
        @($summary.quality_gate.failures).Count -eq 0 -and
        (@($summary.events.terminal_sequence) -join ",") -eq "eof_ack,drained"
      )
    } catch {
      Write-Host "[update] direct stream acceptance returned invalid summary" -ForegroundColor Yellow
      return $false
    }
  } catch {
    Write-Host "[update] direct stream acceptance could not be executed" -ForegroundColor Yellow
    return $false
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

function Invoke-LiveSttStreamAcceptance {
  param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
    [Parameter(Mandatory = $true)][double]$DeadlineSec,
    [string]$Url = "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1",
    [int]$ConnectTimeoutCapSec = 30
  )

  $fixtures = @("sample-tr-cv17-001", "sample-tr-cv17-002")
  for ($index = 0; $index -lt $fixtures.Count; $index++) {
    $fixturesRemaining = $fixtures.Count - $index
    $remainingSec = $DeadlineSec - $Clock.Elapsed.TotalSeconds
    if ($remainingSec -le (5 * $fixturesRemaining)) { return $false }
    $fixtureBudgetSec = [Math]::Floor($remainingSec / $fixturesRemaining)
    $fixtureDeadlineSec = [Math]::Min(
      $DeadlineSec,
      $Clock.Elapsed.TotalSeconds + $fixtureBudgetSec
    )
    if (-not (Invoke-LiveSttFixtureAcceptance -PythonExe $PythonExe `
        -Clock $Clock -DeadlineSec $fixtureDeadlineSec `
        -FixtureBaseName $fixtures[$index] -Url $Url `
        -ConnectTimeoutCapSec $ConnectTimeoutCapSec)) {
      Write-Host ("[update] fixture acceptance failed: {0}" -f $fixtures[$index]) `
        -ForegroundColor Yellow
      return $false
    }
  }
  return $true
}

function New-GpuHostAcceptanceResult {
  param([bool]$Succeeded, [string]$Reason = "")
  return [pscustomobject]@{ Succeeded = $Succeeded; Reason = $Reason }
}

function Invoke-GpuHostRevisionAcceptance {
  param(
    [Parameter(Mandatory = $true)][string]$ExpectedCommit,
    [Parameter(Mandatory = $true)][ValidateSet("strict-v1", "legacy-512e9cc")]
    [string]$AcceptanceProfile
  )

  if ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE -in @(
        "reject-twice", "reject-then-accept"
      )) {
    $script:TestAcceptanceInvocation += 1
    if ($env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE -eq "reject-twice" -or
        $script:TestAcceptanceInvocation -eq 1) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "injected-acceptance-failure"
    }
    return New-GpuHostAcceptanceResult -Succeeded $true -Reason "accepted"
  }

  $liveSttPythonExe = ""
  $liveSttRuntimeOwner = 0
  $liveSttTaskInstance = $null
  $acceptanceClock = [Diagnostics.Stopwatch]::StartNew()
  foreach ($task in @("platform-ai-live-stt", "platform-ai-meeting-ai")) {
    if ((Invoke-SchtasksTask -Action "/Query" -TaskName $task) -ne 0) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-task-missing"
    }
    $taskSpec = Get-GpuHostTaskSpec -TaskName $task
    $taskContract = Get-TaskRuntimeContract -TaskName $task
    if (-not $taskContract.Valid) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-task-contract"
    }
    if (-not (Test-GpuHostSameLocalPath -Left $RepoRoot `
        -Right ([string]$taskContract.RepoRoot))) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-task-repo-root"
    }
    $taskPythonExe = [string]$taskContract.PythonExe
    $previousTaskSnapshot = Get-GpuHostTaskInstanceSnapshot -TaskName $task
    if (-not $previousTaskSnapshot.Succeeded) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-task-query"
    }
    $previousInstanceGuids = @($previousTaskSnapshot.Instances | ForEach-Object {
      ([string]$_.InstanceGuid).ToLowerInvariant()
    })
    $previousOwnerSnapshot = Get-GpuHostListeningPortOwnerSnapshot `
      -Port ([int]$taskSpec.Port)
    if (-not $previousOwnerSnapshot.Succeeded) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-owner-query"
    }
    $previousOwners = @($previousOwnerSnapshot.Owners)
    $endExit = Invoke-SchtasksTask -Action "/End" -TaskName $task
    if ($previousInstanceGuids.Count -gt 0 -and $endExit -ne 0) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-task-end"
    }
    $taskReleased = Wait-GpuHostTaskInstancesReleased -TaskName $task `
      -PreviousInstanceGuids $previousInstanceGuids -Clock $acceptanceClock `
      -DeadlineSec $script:LiveSttReadinessDeadlineSec
    if (-not $taskReleased.Succeeded) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-stale-task-instance"
    }
    $portReleased = Wait-GpuHostPortReleased -Port ([int]$taskSpec.Port) `
      -Clock $acceptanceClock -DeadlineSec $script:LiveSttReadinessDeadlineSec
    if (-not $portReleased.Succeeded) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-stale-listener"
    }
    if ((Invoke-SchtasksTask -Action "/Run" -TaskName $task) -ne 0) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-task-run"
    }
    $newTaskResult = Wait-GpuHostNewTaskInstance -TaskName $task `
      -PreviousInstanceGuids $previousInstanceGuids -Clock $acceptanceClock `
      -DeadlineSec $script:LiveSttReadinessDeadlineSec
    if (-not $newTaskResult.Succeeded -or @($newTaskResult.Instances).Count -ne 1) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-no-new-task-instance"
    }
    $newTaskInstance = $newTaskResult.Instances[0]
    $newOwnerResult = Wait-GpuHostNewPortOwner -Port ([int]$taskSpec.Port) `
      -PreviousOwners $previousOwners -Clock $acceptanceClock `
      -DeadlineSec $script:LiveSttReadinessDeadlineSec
    if (-not $newOwnerResult.Succeeded -or @($newOwnerResult.Owners).Count -ne 1) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-no-new-listener"
    }
    $newOwner = [int]$newOwnerResult.Owners[0]
    $taskPids = @([int]$newTaskInstance.EnginePid)
    if (-not (Test-GpuHostTaskInstanceStable -TaskName $task `
          -ExpectedInstanceGuid ([string]$newTaskInstance.InstanceGuid) `
          -ExpectedEnginePid ([int]$newTaskInstance.EnginePid) `
          -Clock $acceptanceClock -DeadlineSec $script:LiveSttReadinessDeadlineSec) -or
        -not (Test-GpuHostListenerStable -Port ([int]$taskSpec.Port) `
          -ExpectedOwnerId $newOwner -ExpectedPythonExe $taskPythonExe `
          -ExpectedTaskPids $taskPids -Clock $acceptanceClock `
          -DeadlineSec $script:LiveSttReadinessDeadlineSec)) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "restart-failed-identity-unstable"
    }
    if ($task -eq "platform-ai-live-stt") {
      $liveSttPythonExe = $taskPythonExe
      $liveSttRuntimeOwner = $newOwner
      $liveSttTaskInstance = $newTaskInstance
    }
    Write-Host "[update] restarted $task with a new task instance and listener" `
      -ForegroundColor Green
  }

  $streamReady = ($AcceptanceProfile -eq "legacy-512e9cc")
  if (-not $streamReady) {
    Write-Host "[update] waiting for live-stt streaming model readiness..." `
      -ForegroundColor Cyan
  }
  while (-not $streamReady -and
      $acceptanceClock.Elapsed.TotalSeconds -lt $script:LiveSttReadinessDeadlineSec) {
    $remaining = $script:LiveSttReadinessDeadlineSec - $acceptanceClock.Elapsed.TotalSeconds
    $requestTimeout = [Math]::Max(1, [Math]::Min(5, [Math]::Ceiling($remaining)))
    try {
      $readiness = Invoke-RestMethod "http://127.0.0.1:8200/ready" `
        -TimeoutSec $requestTimeout -ErrorAction Stop
      $runtimeOk = (
        $readiness.status -eq "ready" -and
        $readiness.runtime_commit -eq $ExpectedCommit -and
        [int]$readiness.preload_budget_sec -eq $script:LiveSttReadinessDeadlineSec -and
        $readiness.runtime.legacy.device -eq "cpu" -and
        $readiness.runtime.legacy.compute_type -eq "int8" -and
        $readiness.runtime.live.device -eq "cuda" -and
        $readiness.runtime.live.compute_type -eq "int8" -and
        $readiness.runtime.final.device -eq "cuda" -and
        $readiness.runtime.final.compute_type -eq "float16"
      )
      if ($runtimeOk) { $streamReady = $true; break }
    } catch { }
    $sleepMs = [Math]::Min(5000, [Math]::Max(
      0,
      [int](($script:LiveSttReadinessDeadlineSec - $acceptanceClock.Elapsed.TotalSeconds) * 1000)
    ))
    if ($sleepMs -gt 0) { Start-Sleep -Milliseconds $sleepMs }
  }
  if (-not $streamReady) {
    return New-GpuHostAcceptanceResult -Succeeded $false -Reason "readiness-failed"
  }
  $liveTaskPids = @([int]$liveSttTaskInstance.EnginePid)
  if (-not (Test-GpuHostTaskInstanceStable -TaskName "platform-ai-live-stt" `
        -ExpectedInstanceGuid ([string]$liveSttTaskInstance.InstanceGuid) `
        -ExpectedEnginePid ([int]$liveSttTaskInstance.EnginePid) `
        -Clock $acceptanceClock -DeadlineSec $script:LiveSttReadinessDeadlineSec) -or
      -not (Test-GpuHostListenerStable -Port 8200 `
        -ExpectedOwnerId $liveSttRuntimeOwner -ExpectedPythonExe $liveSttPythonExe `
        -ExpectedTaskPids $liveTaskPids -Clock $acceptanceClock `
        -DeadlineSec $script:LiveSttReadinessDeadlineSec)) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "readiness-failed-identity-changed"
  }
  $connectTimeoutCapSec = 30
  if ($AcceptanceProfile -eq "legacy-512e9cc") { $connectTimeoutCapSec = 300 }
  if (-not (Invoke-LiveSttStreamAcceptance -PythonExe $liveSttPythonExe `
      -Clock $acceptanceClock -DeadlineSec $script:LiveSttReadinessDeadlineSec `
      -Url "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1" `
      -ConnectTimeoutCapSec $connectTimeoutCapSec)) {
    return New-GpuHostAcceptanceResult -Succeeded $false -Reason "smoke-failed"
  }
  if (-not (Test-GpuHostTaskInstanceStable -TaskName "platform-ai-live-stt" `
        -ExpectedInstanceGuid ([string]$liveSttTaskInstance.InstanceGuid) `
        -ExpectedEnginePid ([int]$liveSttTaskInstance.EnginePid) `
        -Clock $acceptanceClock -DeadlineSec $script:LiveSttReadinessDeadlineSec) -or
      -not (Test-GpuHostListenerStable -Port 8200 `
        -ExpectedOwnerId $liveSttRuntimeOwner -ExpectedPythonExe $liveSttPythonExe `
        -ExpectedTaskPids $liveTaskPids -Clock $acceptanceClock `
        -DeadlineSec $script:LiveSttReadinessDeadlineSec)) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "smoke-failed-identity-changed"
  }
  return New-GpuHostAcceptanceResult -Succeeded $true -Reason "accepted"
}

function Invoke-GpuHostAutomaticRollback {
  param(
    [Parameter(Mandatory = $true)][string]$RestoreCommit,
    [Parameter(Mandatory = $true)][string]$RejectedResult,
    [AllowNull()][string]$RestorePreviousCommit
  )

  Set-DeploymentLedgerResult -Result $RejectedResult
  Write-Host "[update] revision rejected; restoring $RestoreCommit" `
    -ForegroundColor Yellow
  $restoreOk = (
    (Invoke-GitStream -GitArgs @("checkout", "--detach", $RestoreCommit)) -eq 0 -and
    (Invoke-GitStream -GitArgs @("reset", "--hard", $RestoreCommit)) -eq 0
  )
  if (-not $restoreOk) { return $false }
  $restoreHead = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
  if ($restoreHead.ExitCode -ne 0 -or $restoreHead.Output.Count -ne 1 -or
      "$($restoreHead.Output[0])".Trim().ToLowerInvariant() -ne $RestoreCommit) {
    return $false
  }
  $script:DeploymentLedgerRecord = New-DeploymentStateRecord `
    -CurrentCommit $RestoreCommit -PreviousCommit $RestorePreviousCommit `
    -BranchRef $branchRef `
    -Action "rollback" -Result "automatic-rollback-source-restored"
  try {
    Write-DeploymentStateAtomic -StatePath $StatePath `
      -State $script:DeploymentLedgerRecord
  } catch {
    return $false
  }
  $rollbackProfile = "strict-v1"
  if ($RestoreCommit -eq $script:LegacyRollbackCompatCommit) {
    $rollbackProfile = "legacy-512e9cc"
  }
  $rollbackAcceptance = Invoke-GpuHostRevisionAcceptance `
    -ExpectedCommit $RestoreCommit -AcceptanceProfile $rollbackProfile
  if (-not $rollbackAcceptance.Succeeded) {
    Set-DeploymentLedgerResult `
      -Result ("automatic-rollback-failed-{0}" -f $rollbackAcceptance.Reason)
    return $false
  }
  Set-DeploymentLedgerResult -Result "automatic-rollback-accepted"
  return $true
}

if ($NoRestart) {
  Write-Host "[update] -NoRestart: skipping task restart." -ForegroundColor Yellow
  Set-DeploymentLedgerResult -Result "pinned-no-restart"
} else {
  $targetAcceptanceProfile = "strict-v1"
  if ($Rollback -and $target -eq $script:LegacyRollbackCompatCommit) {
    $targetAcceptanceProfile = "legacy-512e9cc"
  }
  $acceptance = Invoke-GpuHostRevisionAcceptance -ExpectedCommit $target `
    -AcceptanceProfile $targetAcceptanceProfile
  if (-not $acceptance.Succeeded) {
    $restorePreviousCommit = $null
    if ($state) { $restorePreviousCommit = $state.previousCommit }
    if (-not (Invoke-GpuHostAutomaticRollback -RestoreCommit $before `
        -RejectedResult $acceptance.Reason `
        -RestorePreviousCommit $restorePreviousCommit)) {
      Stop-Deploy "Revision acceptance and automatic rollback acceptance failed." `
        $script:DeployExitRollbackFailed
    }
    Stop-Deploy "Revision acceptance failed; previous revision was restored and reaccepted." `
      $script:DeployExitRestartFailed
  }
}

Write-Host "[update] done. Verify: Invoke-RestMethod http://127.0.0.1:8200/health ; :8200/ready ; :8300/health ; ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1 ready" -ForegroundColor Cyan
if (-not $NoRestart) {
  Set-DeploymentLedgerResult -Result "tasks-restarted"
}
if ($script:DeployMutex) {
  if ($script:DeployLockTaken) { $script:DeployMutex.ReleaseMutex() }
  $script:DeployMutex.Dispose()
  $script:DeployMutex = $null
}
exit 0
