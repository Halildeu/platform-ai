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
  Deploy clone path. Defaults to the repo this script lives in (deploy/gpu-host/..).

.PARAMETER Branch
  Tracking branch. Default 'main'. The deploy clone tracks main only.

.PARAMETER StatePath
  Hardened deployment ledger path. Defaults to ProgramData.

.PARAMETER Rollback
  Roll back only to deployment-state.json previousCommit.

.PARAMETER NoRestart
  Pin and ledger the working tree but do not restart scheduled tasks.

.EXAMPLE
  cd C:\Users\denetimpc\platform-ai
  Set-ExecutionPolicy -Scope Process Bypass
  .\deploy\gpu-host\update.ps1 -TargetCommit <full-40-hex-commit>
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
  [string]$RepoRoot = "",
  [string]$Branch = "main",
  [string]$TargetCommit = "",
  [string]$StatePath = "C:\ProgramData\Acik\platform-ai\deployment-state.json",
  [switch]$Rollback,
  [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
$script:DeployExitGuard = 2
$script:DeployExitRestartFailed = 3
$script:DeployExitRollbackFailed = 4
$script:DeployMutex = $null
$script:DeployLockTaken = $false
$script:DefaultDeploymentStatePath = `
  "C:\ProgramData\Acik\platform-ai\deployment-state.json"
$script:TestFaultsEnabled = (
  $env:CI -eq "true" -and
  $StatePath -ne $script:DefaultDeploymentStatePath
)

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
  $scriptDir = $PSScriptRoot
  if ([string]::IsNullOrWhiteSpace($scriptDir) -and $PSCommandPath) {
    $scriptDir = Split-Path -Parent $PSCommandPath
  }
  if ([string]::IsNullOrWhiteSpace($scriptDir) -and $MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  }
  if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    Stop-Deploy "Could not resolve script directory. Pass -RepoRoot explicitly." `
      $script:DeployExitGuard
  }
  $RepoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
}

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
  Stop-Deploy "RepoRoot '$RepoRoot' is not a git clone. Pass -RepoRoot explicitly." `
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

$ledgerRecord = New-DeploymentStateRecord -CurrentCommit $target `
  -PreviousCommit $previous -BranchRef $branchRef -Action $action `
  -Result "source-pinned"
try {
  if ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE -eq "1") {
    throw "CI fault injection: deployment ledger write failure"
  }
  Write-DeploymentStateAtomic -StatePath $StatePath -State $ledgerRecord
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

  $ledgerRecord["lastResult"] = $Result
  $ledgerRecord["timestampUtc"] = [DateTime]::UtcNow.ToString("o")
  try {
    Write-DeploymentStateAtomic -StatePath $StatePath -State $ledgerRecord
  } catch {
    Stop-Deploy ("Pinned source but ledger result update failed: {0}" -f `
      $_.Exception.Message) $script:DeployExitRollbackFailed
  }
}

$runtimeContract = Join-Path $RepoRoot "deploy\gpu-host\live-stt-runtime-contract.ps1"
$taskActionContract = Join-Path $RepoRoot "deploy\gpu-host\task-action-contract.ps1"
if (-not (Test-Path -LiteralPath $runtimeContract -PathType Leaf) -or
    -not (Test-Path -LiteralPath $taskActionContract -PathType Leaf)) {
  Set-DeploymentLedgerResult -Result "restart-failed"
  Stop-Deploy "Pinned source is missing a GPU-host runtime/action contract." `
    $script:DeployExitRestartFailed
}
. $runtimeContract
. $taskActionContract

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

function Get-TaskPythonExe {
  param([Parameter(Mandatory = $true)][string]$TaskName)

  try {
    $query = Get-SchtasksTaskXml -TaskName $TaskName
    if ($query.ExitCode -ne 0 -or $query.Output.Count -eq 0) { return "" }
    [xml]$taskXml = ($query.Output -join [Environment]::NewLine)
    $namespace = New-Object Xml.XmlNamespaceManager($taskXml.NameTable)
    $namespace.AddNamespace("t", $taskXml.DocumentElement.NamespaceURI)
    $exec = $taskXml.SelectSingleNode("//t:Actions/t:Exec", $namespace)
    if ($null -eq $exec) { return "" }
    $contract = Get-GpuHostTaskActionContract -TaskName $TaskName `
      -Execute ([string]$exec.Command) -Arguments ([string]$exec.Arguments) `
      -WorkingDirectory ([string]$exec.WorkingDirectory)
    if (-not $contract.Valid -or
        -not (Test-Path -LiteralPath $contract.PythonExe -PathType Leaf)) {
      return ""
    }
    return [string]$contract.PythonExe
  } catch {
    return ""
  }
}

function Get-ListeningPortOwnerIds {
  param([Parameter(Mandatory = $true)][int]$Port)

  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $lines = @(& netstat.exe -ano -p TCP 2> $null)
    if ($LASTEXITCODE -ne 0) { return @() }
    $pattern = '^\s*TCP\s+\S+:' + $Port + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
    $owners = @()
    foreach ($line in $lines) {
      if ([string]$line -match $pattern) { $owners += [int]$Matches[1] }
    }
    return @($owners | Sort-Object -Unique)
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

function Wait-PortReleased {
  param(
    [Parameter(Mandatory = $true)][int]$Port,
    [int]$TimeoutSec = 30
  )

  $clock = [Diagnostics.Stopwatch]::StartNew()
  while ($clock.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if (@(Get-ListeningPortOwnerIds -Port $Port).Count -eq 0) { return $true }
    Start-Sleep -Milliseconds 250
  }
  return $false
}

function Wait-NewPortOwner {
  param(
    [Parameter(Mandatory = $true)][int]$Port,
    [int[]]$PreviousOwners = @(),
    [int]$TimeoutSec = 60
  )

  $clock = [Diagnostics.Stopwatch]::StartNew()
  while ($clock.Elapsed.TotalSeconds -lt $TimeoutSec) {
    $owners = @(Get-ListeningPortOwnerIds -Port $Port)
    $newOwners = @($owners | Where-Object { $PreviousOwners -notcontains $_ })
    if ($newOwners.Count -gt 0 -and $newOwners.Count -eq $owners.Count) {
      return $newOwners
    }
    Start-Sleep -Milliseconds 250
  }
  return @()
}

function Invoke-LiveSttStreamAcceptance {
  param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [string]$Url = "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1"
  )

  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $smoke = Join-Path $RepoRoot "services\live-stt-service\scripts\live_stream_smoke.py"
    $wav = Join-Path $RepoRoot `
      "services\live-stt-service\tests\fixtures\sample-tr-cv17-001.wav"
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $smoke -PathType Leaf) -or
        -not (Test-Path -LiteralPath $wav -PathType Leaf)) {
      Write-Host "[update] direct stream acceptance interpreter/fixture missing" `
        -ForegroundColor Yellow
      return $false
    }
    $output = & $PythonExe $smoke `
      --url $Url `
      --wav $wav `
      --timeout-sec 30 `
      --final-wait-sec 120 `
      --min-final-word-coverage 0 `
      --min-partial-events 0 `
      --min-final-events 1 `
      --max-transcript-gap-ms 0 2> $null
    if ($LASTEXITCODE -ne 0) {
      Write-Host "[update] direct stream inference acceptance failed" -ForegroundColor Yellow
      return $false
    }
    try {
      $summary = ($output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
      return (
        $summary.ok -eq $true -and
        [int]$summary.events.final_count -ge 1 -and
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

if ($NoRestart) {
  Write-Host "[update] -NoRestart: skipping task restart." -ForegroundColor Yellow
  Set-DeploymentLedgerResult -Result "pinned-no-restart"
} else {
  $restartFailed = $false
  $liveSttPythonExe = ""
  $liveSttRuntimeOwners = @()
  foreach ($task in @("platform-ai-live-stt", "platform-ai-meeting-ai")) {
    if ((Invoke-SchtasksTask -Action "/Query" -TaskName $task) -ne 0) {
      Write-Host "[update] ERROR: required task '$task' is not installed" `
        -ForegroundColor Red
      $restartFailed = $true
      continue
    }
    $taskSpec = Get-GpuHostTaskSpec -TaskName $task
    $taskPythonExe = Get-TaskPythonExe -TaskName $task
    if ([string]::IsNullOrWhiteSpace($taskPythonExe)) {
      Write-Host "[update] ERROR: task '$task' action contract is invalid" `
        -ForegroundColor Red
      $restartFailed = $true
      continue
    }
    $previousOwners = @(Get-ListeningPortOwnerIds -Port ([int]$taskSpec.Port))
    # /End returns non-zero when the task is not running (benign). When it WAS
    # running, require the listener to disappear before /Run. A fixed sleep can
    # accept a stale child or make /Run a no-op while the previous process owns
    # the port.
    $null = Invoke-SchtasksTask -Action "/End" -TaskName $task
    if (-not (Wait-PortReleased -Port ([int]$taskSpec.Port) -TimeoutSec 30)) {
      Write-Host "[update] ERROR: stale listener remained on port $($taskSpec.Port)" `
        -ForegroundColor Red
      $restartFailed = $true
      continue
    }
    $runExit = Invoke-SchtasksTask -Action "/Run" -TaskName $task
    if ($runExit -ne 0) {
      Write-Host "[update] ERROR: schtasks /Run '$task' exit=$runExit" -ForegroundColor Red
      $restartFailed = $true
    } else {
      $newOwners = @(Wait-NewPortOwner -Port ([int]$taskSpec.Port) `
        -PreviousOwners $previousOwners -TimeoutSec 60)
      if ($newOwners.Count -eq 0) {
        Write-Host "[update] ERROR: task '$task' did not create a new listener" `
          -ForegroundColor Red
        $restartFailed = $true
        continue
      }
      if ($task -eq "platform-ai-live-stt") {
        $liveSttPythonExe = $taskPythonExe
        $liveSttRuntimeOwners = $newOwners
      }
      Write-Host "[update] restarted $task with a new port owner" -ForegroundColor Green
    }
  }
  if ($restartFailed) {
    Set-DeploymentLedgerResult -Result "restart-failed"
    Stop-Deploy "Source pin landed but one or more scheduled tasks failed to restart." `
      $script:DeployExitRestartFailed
  }
}

# 5. Wait for both customer-path streaming models. Do not warm the legacy
#    synchronous /transcribe model here: it is a third resident Whisper model,
#    and allocating it during deployment can make the 8 GiB GPU peak unbounded.
#    /transcribe remains lazy; the recording path is accepted by /ready plus the
#    direct WebSocket handshake below.
if (-not $NoRestart -and -not $restartFailed) {
  Write-Host "[update] waiting for live-stt streaming model readiness..." -ForegroundColor Cyan
  $streamReady = $false
  $readinessClock = [Diagnostics.Stopwatch]::StartNew()
  while ($readinessClock.Elapsed.TotalSeconds -lt $script:LiveSttReadinessDeadlineSec) {
    $remaining = $script:LiveSttReadinessDeadlineSec - $readinessClock.Elapsed.TotalSeconds
    $requestTimeout = [Math]::Max(1, [Math]::Min(5, [Math]::Ceiling($remaining)))
    try {
      $readiness = Invoke-RestMethod "http://127.0.0.1:8200/ready" `
        -TimeoutSec $requestTimeout -ErrorAction Stop
      $runtimeOk = (
        $readiness.status -eq "ready" -and
        $readiness.runtime_commit -eq $target -and
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
      [int](($script:LiveSttReadinessDeadlineSec - $readinessClock.Elapsed.TotalSeconds) * 1000)
    ))
    if ($sleepMs -gt 0) { Start-Sleep -Milliseconds $sleepMs }
  }
  if (-not $streamReady) {
    Set-DeploymentLedgerResult -Result "readiness-failed"
    Stop-Deploy ("live-stt exact runtime did not reach /ready within {0}s." -f `
      $script:LiveSttReadinessDeadlineSec) `
      $script:DeployExitRestartFailed
  }
  $currentOwners = @(Get-ListeningPortOwnerIds -Port 8200)
  $ownerDrift = (
    $currentOwners.Count -eq 0 -or
    $currentOwners.Count -ne $liveSttRuntimeOwners.Count -or
    @($currentOwners | Where-Object { $liveSttRuntimeOwners -notcontains $_ }).Count -gt 0 -or
    @($liveSttRuntimeOwners | Where-Object { $currentOwners -notcontains $_ }).Count -gt 0
  )
  if ($ownerDrift) {
    Set-DeploymentLedgerResult -Result "readiness-failed"
    Stop-Deploy "live-stt port owner changed during readiness acceptance." `
      $script:DeployExitRestartFailed
  }
  Write-Host "[update] live-stt streaming models ready" -ForegroundColor Green

  Write-Host "[update] verifying live-stt direct inference + EOF contract..." -ForegroundColor Cyan
  if (Invoke-LiveSttStreamAcceptance -PythonExe $liveSttPythonExe `
      -Url "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1") {
    Write-Host "[update] direct /ws/stream inference + source ranges accepted" -ForegroundColor Green
  } else {
    Set-DeploymentLedgerResult -Result "readiness-failed"
    Stop-Deploy "direct /ws/stream inference/source-range acceptance failed." `
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
