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
  mutation failed. Warmup remains best-effort and transcript-free.

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

function Wait-LiveSttStreamReady {
  param(
    [string]$Url = "ws://127.0.0.1:8200/ws/stream",
    [int]$TimeoutSec = 240
  )

  $oldEap = $ErrorActionPreference
  $client = $null
  try {
    $ErrorActionPreference = "Continue"
    $client = [System.Net.WebSockets.ClientWebSocket]::new()
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    $connectTask = $client.ConnectAsync([Uri]$Url, [Threading.CancellationToken]::None)
    $remainingMs = [Math]::Max(1, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
    if (-not $connectTask.Wait($remainingMs)) {
      Write-Host "[update] direct stream warmup timed out while connecting" -ForegroundColor Yellow
      return $false
    }
    if ($connectTask.Exception) {
      Write-Host "[update] direct stream warmup connect failed: $($connectTask.Exception.GetBaseException().GetType().Name)" -ForegroundColor Yellow
      return $false
    }

    $buffer = New-Object byte[] 8192
    $builder = [System.Text.StringBuilder]::new()
    while ([DateTime]::UtcNow -lt $deadline) {
      $remainingMs = [Math]::Max(1, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
      $segment = [ArraySegment[byte]]::new($buffer)
      $receiveTask = $client.ReceiveAsync($segment, [Threading.CancellationToken]::None)
      if (-not $receiveTask.Wait($remainingMs)) {
        Write-Host "[update] direct stream warmup timed out waiting for ready" -ForegroundColor Yellow
        return $false
      }
      if ($receiveTask.Exception) {
        Write-Host "[update] direct stream warmup receive failed: $($receiveTask.Exception.GetBaseException().GetType().Name)" -ForegroundColor Yellow
        return $false
      }

      $result = $receiveTask.Result
      if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
        Write-Host "[update] direct stream warmup closed before ready" -ForegroundColor Yellow
        return $false
      }

      if ($result.Count -gt 0) {
        [void]$builder.Append([System.Text.Encoding]::UTF8.GetString($buffer, 0, $result.Count))
      }
      if (-not $result.EndOfMessage) {
        continue
      }

      $payload = $builder.ToString()
      [void]$builder.Clear()
      try {
        $event = $payload | ConvertFrom-Json -ErrorAction Stop
        if ($event.type -eq "ready") {
          return $true
        }
        if ($event.type -eq "error") {
          Write-Host "[update] direct stream warmup server error event" -ForegroundColor Yellow
          return $false
        }
      } catch {
        Write-Host "[update] direct stream warmup ignored non-json event" -ForegroundColor Yellow
      }
    }

    Write-Host "[update] direct stream warmup timed out before ready" -ForegroundColor Yellow
    return $false
  } finally {
    if ($client) {
      try {
        if ($client.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
          $null = $client.CloseAsync(
            [System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure,
            "deploy-warmup",
            [Threading.CancellationToken]::None
          ).Wait(2000)
        }
      } catch { }
      $client.Dispose()
    }
    $ErrorActionPreference = $oldEap
  }
}

if ($NoRestart) {
  Write-Host "[update] -NoRestart: skipping task restart." -ForegroundColor Yellow
  Set-DeploymentLedgerResult -Result "pinned-no-restart"
} else {
  $restartFailed = $false
  foreach ($task in @("platform-ai-live-stt", "platform-ai-meeting-ai")) {
    if ((Invoke-SchtasksTask -Action "/Query" -TaskName $task) -ne 0) {
      Write-Host "[update] ERROR: required task '$task' is not installed" `
        -ForegroundColor Red
      $restartFailed = $true
      continue
    }
    # /End returns non-zero when the task is not running (benign). When it WAS
    # running, give the process ~2s to release its listening port before /Run
    # starts a fresh instance (live-stt/meeting-ai bind 8200/8300).
    if ((Invoke-SchtasksTask -Action "/End" -TaskName $task) -eq 0) { Start-Sleep -Seconds 2 }
    $runExit = Invoke-SchtasksTask -Action "/Run" -TaskName $task
    if ($runExit -ne 0) {
      Write-Host "[update] ERROR: schtasks /Run '$task' exit=$runExit" -ForegroundColor Red
      $restartFailed = $true
    } else {
      Write-Host "[update] restarted $task" -ForegroundColor Green
    }
  }
  if ($restartFailed) {
    Set-DeploymentLedgerResult -Result "restart-failed"
    Stop-Deploy "Source pin landed but one or more scheduled tasks failed to restart." `
      $script:DeployExitRestartFailed
  }
}

# 5. Warm live-stt so /health reaches "ok" after the deploy without a manual
#    transcribe (the /transcribe model is lazy-loaded on the first request). This
#    is a plain FOREGROUND curl in update.ps1's own process - NOT a Start-Job: an
#    in-process background job inside the SYSTEM start task breaks the uvicorn
#    launch under WinPS 5.1 (#193 live-acceptance failed). Running it here, outside
#    the service tree, cannot affect the service. Best-effort - never fails update;
#    a reboot (not via this script) stays lazy until the first real transcribe.
if (-not $NoRestart -and -not $restartFailed) {
  $warmupWav = Join-Path $RepoRoot "services\live-stt-service\tests\fixtures\sample-tr-cv17-001.wav"
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ((Test-Path $warmupWav) -and $curl) {
    $oldEap = $ErrorActionPreference
    try {
      $ErrorActionPreference = "Continue"
      Write-Host "[update] warming live-stt (lazy model load)..." -ForegroundColor Cyan
      # Health-wait via Invoke-RestMethod (NOT curl -o $null -w http_code: under
      # WinPS 5.1 a $null arg mangles curl so the code is never "200" and the
      # warmup is always skipped - caught live 2026-06-22). IRM throws on non-200,
      # caught; EAP=Continue is already set so it stays best-effort.
      $up = $false
      for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 5
        try { $null = Invoke-RestMethod "http://127.0.0.1:8200/health" -TimeoutSec 5 -ErrorAction Stop; $up = $true; break } catch { }
      }
      if (-not $up) {
        Write-Host "[update] live-stt /health did not answer in time; skipping warmup (lazy load on first transcribe)" -ForegroundColor Yellow
      } else {
        # -f so an HTTP 4xx/5xx (e.g. a 503 while the model loads) is a non-zero
        # exit rather than a false success; then verify /health actually reached
        # "ok" before logging green (curl exit 0 alone is not warmup acceptance).
        # Match the GPU-host cold-load request budget from start-live-stt.ps1.
        # The old 120s curl cap could abort the deploy warmup before the service
        # reached its own 180s STT_REQUEST_TIMEOUT window.
        & curl.exe -fsS --max-time 240 -F "audio=@$warmupWav;type=audio/wav" "http://127.0.0.1:8200/transcribe?language=tr&session_id=deploy-warmup&meeting_id=deploy-warmup&device_id=deploy-warmup" 1> $null 2> $null
        $curlExit = $LASTEXITCODE
        if ($curlExit -ne 0) {
          Write-Host "[update] live-stt warmup curl exit=$curlExit (service is up; first real transcribe will load it)" -ForegroundColor Yellow
        } else {
          try {
            $health = Invoke-RestMethod "http://127.0.0.1:8200/health" -TimeoutSec 5 -ErrorAction Stop
            if ($health.status -eq "ok") { Write-Host "[update] live-stt warmup posted (model loaded -> /health ok)" -ForegroundColor Green }
            else { Write-Host "[update] live-stt warmup posted but /health status=$($health.status) (first real transcribe may still load it)" -ForegroundColor Yellow }
          } catch {
            Write-Host "[update] live-stt warmup posted but /health verify failed (first real transcribe may still load it)" -ForegroundColor Yellow
          }
        }
      }
    } finally { $ErrorActionPreference = $oldEap }
  }

  Write-Host "[update] warming live-stt direct /ws/stream models..." -ForegroundColor Cyan
  if (Wait-LiveSttStreamReady -Url "ws://127.0.0.1:8200/ws/stream" -TimeoutSec 240) {
    Write-Host "[update] direct /ws/stream warmup ready (live + final models loaded)" -ForegroundColor Green
  } else {
    Write-Host "[update] direct /ws/stream warmup did not reach ready (first stream may pay model load)" -ForegroundColor Yellow
  }
}

Write-Host "[update] done. Verify: Invoke-RestMethod http://127.0.0.1:8200/health ; :8300/health ; ws://127.0.0.1:8200/ws/stream ready" -ForegroundColor Cyan
if (-not $NoRestart) {
  Set-DeploymentLedgerResult -Result "tasks-restarted"
}
if ($script:DeployMutex) {
  if ($script:DeployLockTaken) { $script:DeployMutex.ReleaseMutex() }
  $script:DeployMutex.Dispose()
  $script:DeployMutex = $null
}
exit 0
