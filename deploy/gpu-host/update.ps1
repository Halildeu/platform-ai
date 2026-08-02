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

.PARAMETER ReconcileLedgerDrift
  Recover an out-of-band deploy-clone HEAD drift while deploying an explicit
  approved TargetCommit. The hardened ledger currentCommit remains the rollback
  anchor; the observed drift commit is never adopted as trusted state.

.PARAMETER ControllerCommit
  Optional exact merged origin commit that supplies the updater independently
  from the deployment target. Required with ReconcileLedgerDrift and supported
  for deploy and rollback so an older target never becomes controller authority.

.PARAMETER PreservedPullRequestNumber
  Optional GitHub pull request number whose durable refs/pull/<N>/head ref
  preserves an observed drift commit that is not in origin/main ancestry.
  Requires ReconcileLedgerDrift and PreservedPullRequestHead.

.PARAMETER PreservedPullRequestHead
  Exact full commit expected at refs/pull/<N>/head. The updater fetches that ref
  directly, exact-matches it, and proves the observed deploy HEAD is reachable
  from it before any source mutation. This proof never makes the observed
  commit a trusted deployment or ledger anchor.

.PARAMETER RecoverFencedRuntime
  Explicitly re-enable the two GPU-host scheduled tasks during an attended
  immutable deployment or ledger-drift recovery. A failed acceptance disables
  them again. This switch cannot be combined with NoRestart.

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
  [switch]$ReconcileLedgerDrift,
  [string]$ControllerCommit = "",
  [int]$PreservedPullRequestNumber = 0,
  [string]$PreservedPullRequestHead = "",
  [switch]$RecoverFencedRuntime,
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
$script:TestResultWriteInvocation = 0
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
$hasPreservedPullNumber = ($PreservedPullRequestNumber -gt 0)
$hasPreservedPullHead = -not [string]::IsNullOrWhiteSpace(
  $PreservedPullRequestHead
)
if ($PreservedPullRequestNumber -lt 0 -or
    $hasPreservedPullNumber -ne $hasPreservedPullHead) {
  Stop-Deploy ("Preserved pull proof requires both a positive " +
    "-PreservedPullRequestNumber and -PreservedPullRequestHead.") `
    $script:DeployExitGuard
}
$hasPreservedPullProof = $hasPreservedPullNumber -and $hasPreservedPullHead
if ($hasPreservedPullProof -and -not $ReconcileLedgerDrift) {
  Stop-Deploy "Preserved pull proof requires -ReconcileLedgerDrift." `
    $script:DeployExitGuard
}
if ($hasPreservedPullProof) {
  if ($PreservedPullRequestHead.ToLowerInvariant() -notmatch
      '^[0-9a-f]{40}$') {
    Stop-Deploy "-PreservedPullRequestHead requires exactly 40 hex characters." `
      $script:DeployExitGuard
  }
  $PreservedPullRequestHead = $PreservedPullRequestHead.ToLowerInvariant()
}

$stateModule = Join-Path $PSScriptRoot "deployment-state.ps1"
if (-not (Test-Path -LiteralPath $stateModule -PathType Leaf)) {
  Stop-Deploy "Missing deployment-state.ps1 beside update.ps1." `
    $script:DeployExitGuard
}
. $stateModule

# Durable acceptance evidence. Depends on the state module above for the
# hardened ACL, so it is sourced after it.
$receiptModule = Join-Path $PSScriptRoot "acceptance-receipt.ps1"
if (-not (Test-Path -LiteralPath $receiptModule -PathType Leaf)) {
  Stop-Deploy "Missing acceptance-receipt.ps1 beside update.ps1." `
    $script:DeployExitGuard
}
. $receiptModule

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
$preservedPullLocalRef = ""

$originCheck = Invoke-GitCapture -GitArgs @(
  "rev-parse", "--verify", "--quiet", $originRemoteRef
)
if ($originCheck.ExitCode -ne 0) {
  Stop-Deploy "$originRef not found after fetch. No mutation." `
    $script:DeployExitGuard
}

if ($hasPreservedPullProof) {
  $preservedPullRemoteRef = "refs/pull/{0}/head" -f `
    $PreservedPullRequestNumber
  $preservedPullLocalRef = "refs/remotes/origin/preserved-pull/{0}/head" -f `
    $PreservedPullRequestNumber
  $preservedPullRefSpec = "+{0}:{1}" -f `
    $preservedPullRemoteRef, $preservedPullLocalRef
  if ((Invoke-GitStream -GitArgs @(
      "fetch", "--no-tags", "origin", $preservedPullRefSpec
    )) -ne 0) {
    Stop-Deploy "Declared preserved pull ref could not be fetched. No mutation." `
      $script:DeployExitGuard
  }
  $preservedPullObjectSpec = $preservedPullLocalRef + "^{commit}"
  $preservedPullObject = Invoke-GitCapture -GitArgs @(
    "rev-parse", "--verify", $preservedPullObjectSpec
  )
  if ($preservedPullObject.ExitCode -ne 0 -or
      $preservedPullObject.Output.Count -ne 1 -or
      "$($preservedPullObject.Output[0])".Trim().ToLowerInvariant() -ne
        $PreservedPullRequestHead) {
    Stop-Deploy "Declared preserved pull ref does not exact-match its expected head." `
      $script:DeployExitGuard
  }
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
$untrackedResult = Invoke-GitCapture -GitArgs @(
  "ls-files", "--others", "--exclude-standard"
)
if ($untrackedResult.ExitCode -ne 0) {
  Stop-Deploy "git untracked-content scan failed. No mutation." `
    $script:DeployExitGuard
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
if ($hasPreservedPullProof -and $untrackedResult.Output.Count -gt 0) {
  Stop-Deploy ("Preserved-pull recovery requires zero untracked deployed " +
    "files. Run preserve-untracked.ps1 and verify its restricted receipt.") `
    $script:DeployExitGuard
}
if ($unpushedResult.Output.Count -gt 0 -and -not $hasPreservedPullProof) {
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
if ($Rollback -and $ReconcileLedgerDrift) {
  Stop-Deploy "-Rollback cannot be combined with -ReconcileLedgerDrift." `
    $script:DeployExitGuard
}
if ($ReconcileLedgerDrift -and $NoRestart) {
  Stop-Deploy "-ReconcileLedgerDrift requires restart and runtime acceptance." `
    $script:DeployExitGuard
}
if ($RecoverFencedRuntime -and $NoRestart) {
  Stop-Deploy "-RecoverFencedRuntime requires restart and runtime acceptance." `
    $script:DeployExitGuard
}
$ledgerDriftDetected = ($state -and $state.currentCommit -ne $before)
if ($ReconcileLedgerDrift -and -not $state) {
  Stop-Deploy "-ReconcileLedgerDrift requires an existing valid deployment ledger." `
    $script:DeployExitGuard
}
if ($ledgerDriftDetected -and -not $ReconcileLedgerDrift) {
  Stop-Deploy "HEAD does not match deployment ledger currentCommit. No mutation." `
    $script:DeployExitGuard
}
if ($ReconcileLedgerDrift -and -not $ledgerDriftDetected) {
  Stop-Deploy "-ReconcileLedgerDrift requires an actual HEAD/ledger mismatch." `
    $script:DeployExitGuard
}
if ($ReconcileLedgerDrift -and [string]::IsNullOrWhiteSpace($ControllerCommit)) {
  Stop-Deploy "-ReconcileLedgerDrift requires -ControllerCommit." `
    $script:DeployExitGuard
}
if (-not [string]::IsNullOrWhiteSpace($ControllerCommit)) {
  if ($ControllerCommit.ToLowerInvariant() -notmatch '^[0-9a-f]{40}$') {
    Stop-Deploy "-ControllerCommit requires exactly 40 hex characters." `
      $script:DeployExitGuard
  }
  $ControllerCommit = $ControllerCommit.ToLowerInvariant()
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
    if ($ReconcileLedgerDrift -and $target -eq $state.currentCommit) {
      $previous = $state.previousCommit
    }
    elseif ($ReconcileLedgerDrift) { $previous = $state.currentCommit }
    elseif ($target -eq $before) { $previous = $state.previousCommit }
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
if ($ReconcileLedgerDrift) {
  if (-not ([string]$state.branchRef).Equals(
      $branchRef,
      [StringComparison]::OrdinalIgnoreCase
    )) {
    Stop-Deploy "Deployment ledger branchRef does not match the requested branch. No mutation." `
      $script:DeployExitGuard
  }
  $observedAncestor = Invoke-GitCapture -GitArgs @(
    "merge-base", "--is-ancestor", $before, $originRef
  )
  if ($hasPreservedPullProof) {
    $observedPreserved = Invoke-GitCapture -GitArgs @(
      "merge-base", "--is-ancestor", $before, $preservedPullLocalRef
    )
    if ($observedPreserved.ExitCode -ne 0) {
      Stop-Deploy ("Observed drift HEAD is not reachable from the exact " +
        "preserved pull ref. No mutation.") $script:DeployExitGuard
    }
    Write-Host ("[update] preserved pull proof verified; " +
      "observed HEAD remains untrusted") -ForegroundColor Cyan
  } elseif ($observedAncestor.ExitCode -ne 0) {
    Stop-Deploy "Observed drift HEAD is not an ancestor of $originRef. No mutation." `
      $script:DeployExitGuard
  }
  $ledgerObjectSpec = $state.currentCommit + "^{commit}"
  $ledgerObject = Invoke-GitCapture -GitArgs @(
    "rev-parse", "--verify", $ledgerObjectSpec
  )
  $ledgerAncestor = Invoke-GitCapture -GitArgs @(
    "merge-base", "--is-ancestor", $state.currentCommit, $originRef
  )
  if ($ledgerObject.ExitCode -ne 0 -or $ledgerObject.Output.Count -ne 1 -or
      "$($ledgerObject.Output[0])".Trim().ToLowerInvariant() -ne
        $state.currentCommit -or $ledgerAncestor.ExitCode -ne 0) {
    Stop-Deploy "Ledger recovery anchor is unavailable or outside origin ancestry. No mutation." `
      $script:DeployExitGuard
  }
}
if (-not [string]::IsNullOrWhiteSpace($ControllerCommit)) {
  $controllerObjectSpec = $ControllerCommit + "^{commit}"
  $controllerObject = Invoke-GitCapture -GitArgs @(
    "rev-parse", "--verify", $controllerObjectSpec
  )
  $controllerAncestor = Invoke-GitCapture -GitArgs @(
    "merge-base", "--is-ancestor", $ControllerCommit, $originRef
  )
  if ($controllerObject.ExitCode -ne 0 -or
      $controllerObject.Output.Count -ne 1 -or
      "$($controllerObject.Output[0])".Trim().ToLowerInvariant() -ne
        $ControllerCommit -or $controllerAncestor.ExitCode -ne 0) {
    Stop-Deploy "Controller commit is unavailable or outside origin ancestry. No mutation." `
      $script:DeployExitGuard
  }
}

$controllerHead = Invoke-GitCapture -GitArgs @(
  "-C", $controllerRoot, "rev-parse", "HEAD"
)
$expectedControllerCommit = if (-not [string]::IsNullOrWhiteSpace(
    $ControllerCommit
  )) {
  $ControllerCommit
} elseif ($Rollback) {
  $before
} else {
  $target
}
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

$shouldProcessAction = if ($ReconcileLedgerDrift) {
  "reconcile observed HEAD drift to ledger anchor and deploy"
} else {
  $action
}
if (-not $PSCmdlet.ShouldProcess(
    $RepoRoot,
    ("{0} immutable commit {1}" -f $shouldProcessAction, $target)
  )) {
  Write-Host "[update] WhatIf/declined: validation passed; no mutation." `
    -ForegroundColor Yellow
  $script:DeployMutex.ReleaseMutex()
  $script:DeployMutex.Dispose()
  $script:DeployMutex = $null
  exit 0
}

function Restore-GpuHostTrustedDeploymentState {
  param([AllowNull()]$TrustedState)

  try {
    if ($null -eq $TrustedState) {
      if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction Stop
      }
      return (-not (Test-Path -LiteralPath $StatePath -PathType Leaf))
    }

    Write-DeploymentStateAtomic -StatePath $StatePath -State $TrustedState
    $verified = Read-DeploymentState -StatePath $StatePath
    if ($null -eq $verified) { return $false }
    foreach ($field in @(
        "schemaVersion", "currentCommit", "previousCommit", "timestampUtc",
        "branchRef", "lastAction", "lastResult", "host"
      )) {
      if ([string]$verified.$field -cne [string]$TrustedState.$field) {
        return $false
      }
    }
    return $true
  } catch {
    Write-Host ("[update] trusted ledger restore failed: {0}" -f `
      $_.Exception.Message) -ForegroundColor Red
    return $false
  }
}

function Invoke-GpuHostSourceAndLedgerMutation {
  $pinFailedCode = $script:DeployExitRollbackFailed
  $pinFailureReason = ""
  $checkoutExit = Invoke-GitStream -GitArgs @("checkout", "--detach", $target)
  if ($checkoutExit -ne 0) {
    $pinFailureReason = "checkout-failed"
  } else {
    $resetExit = 0
    if ($script:TestFaultsEnabled -and
        $env:PLATFORM_AI_TEST_INJECT_PIN_RESET_FAILURE -eq "1") {
      $resetExit = 1
    } else {
      $resetExit = Invoke-GitStream -GitArgs @("reset", "--hard", $target)
    }
    if ($resetExit -ne 0) { $pinFailureReason = "reset-failed" }
  }

  if ([string]::IsNullOrWhiteSpace($pinFailureReason)) {
    $afterResult = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
    $symbolicResult = Invoke-GitCapture -GitArgs @("symbolic-ref", "-q", "HEAD")
    if ($script:TestFaultsEnabled -and
        $env:PLATFORM_AI_TEST_INJECT_PIN_POSTCONDITION_FAILURE -eq "1") {
      $pinFailureReason = "postcondition-failed"
    } elseif ($afterResult.ExitCode -ne 0 -or
        $afterResult.Output.Count -ne 1 -or
        "$($afterResult.Output[0])".Trim().ToLowerInvariant() -ne $target -or
        $symbolicResult.ExitCode -eq 0) {
      $pinFailureReason = "postcondition-failed"
    }
  }

  if (-not [string]::IsNullOrWhiteSpace($pinFailureReason)) {
    $pinRestoreCommit = $before
    if ($ReconcileLedgerDrift) {
      $pinRestoreCommit = $state.currentCommit
    }
    $pinRecovery = Invoke-GpuHostTrustedPinFailureRecovery `
      -RestoreCommit $pinRestoreCommit -TrustedState $state `
      -RequireAcceptance (-not $NoRestart)
    if ($pinRecovery.Succeeded) {
      $proof = "trusted source/ledger restored"
      if (-not $NoRestart) { $proof += " and runtime reaccepted" }
      Stop-Deploy ("Target pin failed ({0}); {1}." -f `
        $pinFailureReason, $proof) $pinFailedCode
    }
    $fenced = Stop-GpuHostRuntimeFailClosed
    $fenceResult = if ($fenced) { "runtime fenced" } else {
      "runtime fence could not be proven"
    }
    Stop-Deploy ("Target pin failed ({0}); trusted recovery failed ({1}); {2}." -f `
      $pinFailureReason, $pinRecovery.Reason, $fenceResult) $pinFailedCode
  }

  $script:DeploymentLedgerRecord = New-DeploymentStateRecord `
    -CurrentCommit $target -PreviousCommit $previous -BranchRef $branchRef `
    -Action $action -Result "source-pinned"
  try {
    if ($script:TestFaultsEnabled -and
        $env:PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE -eq "1") {
      throw "CI fault injection: deployment ledger write failure"
    }
    Write-DeploymentStateAtomic -StatePath $StatePath `
      -State $script:DeploymentLedgerRecord
    if ($script:TestFaultsEnabled -and $ReconcileLedgerDrift -and
        $env:PLATFORM_AI_TEST_INJECT_LEDGER_POST_WRITE_FAILURE -eq "1") {
      throw "CI fault injection: post-write deployment ledger failure"
    }
  } catch {
    $ledgerWriteError = $_.Exception.Message
    $ledgerWriteRestoreCommit = $before
    if ($ReconcileLedgerDrift) {
      $ledgerWriteRestoreCommit = $state.currentCommit
    }
    Write-Host ("[update] ledger write failed; restoring trusted commit {0}" -f `
      $ledgerWriteRestoreCommit) -ForegroundColor Red
    $restoreOk = $false
    if (-not ($script:TestFaultsEnabled -and
        $env:PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE -eq "1")) {
      $restoreOk = ((Invoke-GitStream -GitArgs @(
        "checkout", "--detach", $ledgerWriteRestoreCommit
      )) -eq 0)
    }
    if ($restoreOk) {
      $restoreOk = ((Invoke-GitStream -GitArgs @(
        "reset", "--hard", $ledgerWriteRestoreCommit
      )) -eq 0)
    }
    if ($restoreOk) {
      $restoreHead = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
      $restoreSymbolic = Invoke-GitCapture -GitArgs @(
        "symbolic-ref", "-q", "HEAD"
      )
      $restoreOk = ($restoreHead.ExitCode -eq 0 -and
        $restoreHead.Output.Count -eq 1 -and
        "$($restoreHead.Output[0])".Trim().ToLowerInvariant() -eq
          $ledgerWriteRestoreCommit -and
        $restoreSymbolic.ExitCode -ne 0)
    }
    if (-not $restoreOk) {
      $fenced = Stop-GpuHostRuntimeFailClosed
      $fenceResult = if ($fenced) { "runtime fenced" } else {
        "runtime fence could not be proven"
      }
      Stop-Deploy ("Ledger write and automatic source restoration failed; {0}." -f `
        $fenceResult) `
        $script:DeployExitRollbackFailed
    }
    if (-not (Restore-GpuHostTrustedDeploymentState -TrustedState $state)) {
      $fenced = Stop-GpuHostRuntimeFailClosed
      $fenceResult = if ($fenced) { "runtime fenced" } else {
        "runtime fence could not be proven"
      }
      Stop-Deploy ("Ledger write failed; source restored but trusted ledger could not be restored and verified; {0}." -f `
        $fenceResult) $script:DeployExitRollbackFailed
    }
    if ($ReconcileLedgerDrift) {
      $restoreProfile = "strict-v1"
      if ($ledgerWriteRestoreCommit -eq $script:LegacyRollbackCompatCommit) {
        $restoreProfile = "legacy-512e9cc"
      }
      try {
        $restoreAcceptance = Invoke-GpuHostRevisionAcceptance `
          -ExpectedCommit $ledgerWriteRestoreCommit `
          -AcceptanceProfile $restoreProfile
      } catch {
        $restoreAcceptance = New-GpuHostAcceptanceResult -Succeeded $false `
          -Reason ("acceptance-exception-{0}" -f $_.Exception.GetType().Name)
      }
      if (-not $restoreAcceptance.Succeeded) {
        $stopped = Stop-GpuHostRuntimeFailClosed
        $stopResult = if ($stopped) { "runtime fenced" } else {
          "runtime fence could not be proven"
        }
        Stop-Deploy ("Ledger write failed; trusted source restored but runtime acceptance failed ({0}); {1}: {2}" -f `
          $restoreAcceptance.Reason, $stopResult, $ledgerWriteError) `
          $script:DeployExitRollbackFailed
      }
      Stop-Deploy ("Ledger write failed; trusted source restored and runtime reaccepted: {0}" -f `
        $ledgerWriteError) $script:DeployExitGuard
    }
    Stop-Deploy ("Ledger write failed; source restored: {0}" -f `
      $ledgerWriteError) $script:DeployExitGuard
  }
  Write-Host "[update] $before -> $target (detached immutable pin)" `
    -ForegroundColor Green
  if ($ReconcileLedgerDrift) {
    Write-Host ("[update] ledger drift reconciled; trusted rollback anchor={0}" -f `
      $previous) -ForegroundColor Green
  }
}

function Set-DeploymentLedgerResult {
  param([Parameter(Mandatory = $true)][string]$Result)

  $script:TestResultWriteInvocation += 1
  $faultPoint = "{0}-pre" -f $script:TestResultWriteInvocation
  if ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_INJECT_RESULT_WRITE_FAILURE -eq $faultPoint) {
    throw ("CI fault injection: result write failure at {0}" -f $faultPoint)
  }
  $script:DeploymentLedgerRecord["lastResult"] = $Result
  $script:DeploymentLedgerRecord["timestampUtc"] = [DateTime]::UtcNow.ToString("o")
  Write-DeploymentStateAtomic -StatePath $StatePath `
    -State $script:DeploymentLedgerRecord
  $faultPoint = "{0}-post" -f $script:TestResultWriteInvocation
  if ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_INJECT_RESULT_WRITE_FAILURE -eq $faultPoint) {
    throw ("CI fault injection: result post-write failure at {0}" -f `
      $faultPoint)
  }
}

$runtimeContract = Join-Path $controllerRoot "deploy\gpu-host\live-stt-runtime-contract.ps1"
$meetingAiRuntimeContract = Join-Path $controllerRoot `
  "deploy\gpu-host\meeting-ai-runtime-env.ps1"
$taskActionContract = Join-Path $controllerRoot "deploy\gpu-host\task-action-contract.ps1"
$bootstrapProcess = Join-Path $controllerRoot "deploy\gpu-host\bootstrap-process.ps1"
$restartAcceptance = Join-Path $controllerRoot "deploy\gpu-host\restart-acceptance.ps1"
if (-not (Test-Path -LiteralPath $runtimeContract -PathType Leaf) -or
    -not (Test-Path -LiteralPath $meetingAiRuntimeContract -PathType Leaf) -or
    -not (Test-Path -LiteralPath $taskActionContract -PathType Leaf) -or
    -not (Test-Path -LiteralPath $bootstrapProcess -PathType Leaf) -or
    -not (Test-Path -LiteralPath $restartAcceptance -PathType Leaf)) {
  Stop-Deploy "Controller is missing a GPU-host runtime/action contract. No mutation." `
    $script:DeployExitGuard
}
. $runtimeContract
. $meetingAiRuntimeContract
. $taskActionContract
. $bootstrapProcess
. $restartAcceptance

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

function Set-SchtasksTaskEnabled {
  param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][bool]$Enabled
  )

  $mode = if ($Enabled) { "/Enable" } else { "/Disable" }
  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & schtasks.exe /Change /TN $TaskName $mode 1> $null 2> $null
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

function Get-GpuHostManagedTaskNames {
  if ($script:TestFaultsEnabled -and
      -not [string]::IsNullOrWhiteSpace(
        $env:PLATFORM_AI_TEST_FAIL_CLOSED_TASK_NAMES
      )) {
    $names = @(
      $env:PLATFORM_AI_TEST_FAIL_CLOSED_TASK_NAMES.Split(",") |
        ForEach-Object { $_.Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($names.Count -ne 2 -or @($names | Where-Object {
          $_ -notmatch '^platform-ai-ci-[A-Za-z0-9-]+$'
        }).Count -gt 0) {
      throw "Invalid CI fail-closed task-name seam."
    }
    return $names
  }
  return @("platform-ai-live-stt", "platform-ai-meeting-ai")
}

function Get-GpuHostManagedPorts {
  if ($script:TestFaultsEnabled -and
      -not [string]::IsNullOrWhiteSpace(
        $env:PLATFORM_AI_TEST_FAIL_CLOSED_PORTS
      )) {
    $ports = @()
    foreach ($value in $env:PLATFORM_AI_TEST_FAIL_CLOSED_PORTS.Split(",")) {
      $parsed = 0
      if (-not [int]::TryParse($value.Trim(), [ref]$parsed) -or
          $parsed -lt 49152 -or $parsed -gt 65535) {
        throw "Invalid CI fail-closed port seam."
      }
      $ports += $parsed
    }
    if ($ports.Count -ne 2) {
      throw "Invalid CI fail-closed port seam."
    }
    return $ports
  }
  return @(8200, 8300)
}

function Test-SchtasksTaskEnabledState {
  param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][bool]$ExpectedEnabled
  )

  try {
    $query = Get-SchtasksTaskXml -TaskName $TaskName
    if ($query.ExitCode -ne 0 -or $query.Output.Count -eq 0) {
      return $false
    }
    [xml]$taskXml = $query.Output -join [Environment]::NewLine
    $enabledNodes = @($taskXml.Task.Settings.ChildNodes | Where-Object {
      $_.LocalName -eq "Enabled"
    })
    $actual = $true
    if ($enabledNodes.Count -eq 1) {
      $actual = [Convert]::ToBoolean([string]$enabledNodes[0].InnerText)
    } elseif ($enabledNodes.Count -gt 1) {
      return $false
    }
    return ($actual -eq $ExpectedEnabled)
  } catch {
    return $false
  }
}

function Set-GpuHostRuntimeTasksEnabled {
  param([Parameter(Mandatory = $true)][bool]$Enabled)

  try {
    $tasks = @(Get-GpuHostManagedTaskNames)
    $changed = $true
    foreach ($task in $tasks) {
      if ((Set-SchtasksTaskEnabled -TaskName $task -Enabled $Enabled) -ne 0) {
        $changed = $false
      }
    }
    foreach ($task in $tasks) {
      if (-not (Test-SchtasksTaskEnabledState -TaskName $task `
          -ExpectedEnabled $Enabled)) {
        $changed = $false
      }
    }
    return $changed
  } catch {
    return $false
  }
}

function Test-GpuHostRuntimeTaskFencePresent {
  try {
    foreach ($task in @(Get-GpuHostManagedTaskNames)) {
      $query = Get-SchtasksTaskXml -TaskName $task
      if ($query.ExitCode -eq 0 -and $query.Output.Count -gt 0 -and
          -not (Test-SchtasksTaskEnabledState -TaskName $task `
            -ExpectedEnabled $true)) {
        return $true
      }
    }
  } catch {
    return $true
  }
  return $false
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
    [int]$ConnectTimeoutCapSec = 30,
    [ValidateRange(30, 600)][int]$SmokeProcessTimeoutCapSec = 240,
    # Content and draft-path are proven by separate runs because no single
    # stream can judge both fairly on this fixture set:
    #
    #   * One 5.5s clip gives the draft pass about two chances and both are
    #     legitimately filtered as low-confidence, so requiring >=1 partial from
    #     it is a coin flip - measured 1/4 passes on the GPU host.
    #   * Repeating the clip fixes that (4/4 partials) but the tiled seams push
    #     word error rate to 0.20-0.29 against the tiled reference, so content
    #     cannot be judged on the repeated stream.
    #
    # Single pass therefore judges content (measured 4/4 at WER 0.0) and the
    # repeated pass judges only that the draft path emits.
    [int]$RepeatAudio = 1,
    [switch]$DraftPathOnly,
    # Per-fixture, because this gate proves the PINNED model still behaves as it
    # did when it was pinned - it is not a model-quality benchmark. Measured on
    # the GPU host, deterministic across runs:
    #
    #   sample-tr-cv17-001 -> WER 0.000  (4/4 runs)
    #   sample-tr-cv17-002 -> WER 0.375  (3/3 runs)
    #
    # 002 is harder: large-v3-turbo splits "halterci" into "hal tercih", one
    # error against a 7-word reference. The reference is correct Turkish and is
    # NOT edited to match the model - the threshold carries the known gap
    # instead, and one further error would still fail it.
    [double]$MaxWordErrorRate = 0.25
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
    if ($DraftPathOnly) {
      $gateMinPartialEvents = 1
      $gateMinFinalWordCoverage = 0.0
      $gateMinReferenceTokenCoverage = 0.0
      $gateMaxWordErrorRate = 1.0
      $gateMaxTranscriptGapMs = 0
    } else {
      $gateMinPartialEvents = 0
      $gateMinFinalWordCoverage = 0.8
      $gateMinReferenceTokenCoverage = 0.8
      $gateMaxWordErrorRate = $MaxWordErrorRate
      $gateMaxTranscriptGapMs = 6000
    }
    $arguments = @(
      $smoke,
      "--url", $Url,
      "--wav", $wav,
      "--reference-text", $referenceText,
      "--repeat-audio", "$RepeatAudio",
      "--timeout-sec", "$connectTimeoutSec",
      "--final-wait-sec", "$finalWaitSec",
      "--min-final-word-coverage", "$gateMinFinalWordCoverage",
      "--min-partial-events", "$gateMinPartialEvents",
      "--min-final-events", "1",
      "--min-reference-token-coverage", "$gateMinReferenceTokenCoverage",
      "--max-word-error-rate", "$gateMaxWordErrorRate",
      "--max-transcript-gap-ms", "$gateMaxTranscriptGapMs"
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
    # Drain both redirected pipes while the child runs. Waiting for process
    # exit first can deadlock once a repeated-fixture JSON summary fills the
    # finite stdout pipe buffer.
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    [int]$smokeProcessTimeoutSec = [Math]::Max(1, [Math]::Min(
      $SmokeProcessTimeoutCapSec,
      [Math]::Floor($DeadlineSec - $Clock.Elapsed.TotalSeconds)
    ))
    if (-not $process.WaitForExit($smokeProcessTimeoutSec * 1000)) {
      try {
        Stop-GpuHostProcessTreeBounded -Process $process -GraceSec 10
      } catch {
        Write-Host "[update] timed-out smoke process tree could not be fenced" `
          -ForegroundColor Yellow
      }
      $process.Dispose()
      Write-Host ("[update] direct stream inference acceptance timed out after {0}s" -f `
        $smokeProcessTimeoutSec) -ForegroundColor Yellow
      Write-GpuHostAcceptanceReceipt -Fixture $FixtureBaseName `
        -RepeatAudio $RepeatAudio -DraftPathOnly ([bool]$DraftPathOnly) `
        -Verdict "smoke-process-timeout" `
        -FailedChecks @("smoke_process_timeout")
      return $false
    }
    $output = $stdoutTask.GetAwaiter().GetResult()
    [void]$stderrTask.GetAwaiter().GetResult()
    $exitCode = $process.ExitCode
    $process.Dispose()
    if ($exitCode -ne 0 -or
        -not (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec)) {
      Write-Host ("[update] direct stream inference acceptance failed (exit {0})" -f `
        $exitCode) -ForegroundColor Yellow
      Write-GpuHostAcceptanceReceipt -Fixture $FixtureBaseName `
        -RepeatAudio $RepeatAudio -DraftPathOnly ([bool]$DraftPathOnly) `
        -Verdict "smoke-process-failed" `
        -FailedChecks @("smoke_exit_code_or_deadline")
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
      # Named, not one 30-term -and chain. The chain short-circuited to a bare
      # $false and printed nothing, so a refused deploy could not be told apart
      # from a crashed one without re-running it by hand - which is how a
      # deploy stalled on 2026-07-27 with the gate returning false while the
      # same fixture passed when run manually. Each term now carries the name
      # that appears in the console line and in the receipt.
      $checks = [ordered]@{
        schema =
          $summary.schema -eq "platform-ai.live-stt.stream-smoke.v1"
        smoke_ok =
          $summary.ok -eq $true
        reference_artifact_hash =
          $summary.reference.artifact_id_sha256_12 -eq $referenceNameHash
        reference_text_hash =
          $summary.reference.text_sha256_12 -eq $referenceTextHash
        reference_words =
          [int]$summary.reference.words -ge 1
        partial_events =
          (-not $DraftPathOnly -or [int]$summary.events.partial_count -ge 1)
        final_events =
          [int]$summary.events.final_count -ge 1
        no_final_hallucination =
          [int]$summary.events.final_hallucination_count -eq 0
        no_stream_errors =
          [int]$summary.events.error_count -eq 0
        transcript_gap_shape =
          ($null -eq $summary.events.max_transcript_gap_ms -or
           [int]$summary.events.max_transcript_gap_ms -ge 0)
        transcript_gap_within_max =
          ($DraftPathOnly -or
           $null -eq $summary.events.max_transcript_gap_ms -or
           [int]$summary.events.max_transcript_gap_ms -le 6000)
        coverage_reference_words =
          [int]$summary.coverage.reference_words -eq [int]$summary.reference.words
        coverage_final_words =
          [int]$summary.coverage.final_words -ge 1
        fixture_repeat_audio =
          [int]$summary.fixture.repeat_audio -eq $RepeatAudio
        content_quality =
          ($DraftPathOnly -or (
            [double]$summary.coverage.final_word_coverage -ge 0.8 -and
            [double]$summary.coverage.reference_token_coverage -ge 0.8 -and
            [double]$summary.coverage.word_error_rate -ge 0.0 -and
            [double]$summary.coverage.word_error_rate -le $MaxWordErrorRate
          ))
        gate_min_partial_events =
          [int]$summary.quality_gate.min_partial_events -eq $gateMinPartialEvents
        gate_min_final_events =
          [int]$summary.quality_gate.min_final_events -eq 1
        gate_min_final_word_coverage =
          [double]$summary.quality_gate.min_final_word_coverage -eq `
            $gateMinFinalWordCoverage
        gate_min_reference_token_coverage =
          [double]$summary.quality_gate.min_reference_token_coverage -eq `
            $gateMinReferenceTokenCoverage
        gate_max_word_error_rate =
          [double]$summary.quality_gate.max_word_error_rate -eq $gateMaxWordErrorRate
        gate_max_transcript_gap_ms =
          [int]$summary.quality_gate.max_transcript_gap_ms -eq $gateMaxTranscriptGapMs
        smoke_reported_no_failures =
          @($summary.quality_gate.failures).Count -eq 0
        terminal_sequence =
          (@($summary.events.terminal_sequence) -join ",") -eq "eof_ack,drained"
      }
      $failed = @($checks.Keys | Where-Object { -not $checks[$_] })
      $verdict = "accepted"
      if ($failed.Count -gt 0) { $verdict = "rejected" }
      Write-GpuHostAcceptanceReceipt -Fixture $FixtureBaseName `
        -RepeatAudio $RepeatAudio -DraftPathOnly ([bool]$DraftPathOnly) `
        -Verdict $verdict -FailedChecks $failed -Summary $summary
      if ($failed.Count -gt 0) {
        Write-Host ("[update] direct stream acceptance rejected by: {0}" -f `
          ($failed -join ", ")) -ForegroundColor Yellow
        return $false
      }
      return $true
    } catch {
      Write-Host ("[update] direct stream acceptance returned invalid summary: {0}" -f `
        $_.Exception.GetType().Name) -ForegroundColor Yellow
      Write-GpuHostAcceptanceReceipt -Fixture $FixtureBaseName `
        -RepeatAudio $RepeatAudio -DraftPathOnly ([bool]$DraftPathOnly) `
        -Verdict "invalid-summary" `
        -FailedChecks @("summary_parse_or_shape")
      return $false
    }
  } catch {
    Write-Host "[update] direct stream acceptance could not be executed" -ForegroundColor Yellow
    Write-GpuHostAcceptanceReceipt -Fixture $FixtureBaseName `
      -RepeatAudio $RepeatAudio -DraftPathOnly ([bool]$DraftPathOnly) `
      -Verdict "not-executed" -FailedChecks @("acceptance_invocation")
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

  # Two content runs (single pass, full content gates) plus one draft-path run
  # (repeated pass, partial gate only). See Invoke-LiveSttFixtureAcceptance for
  # why neither property can be judged fairly on the other's stream.
  $runs = @(
    @{ Fixture = "sample-tr-cv17-001"; Repeat = 1; DraftPathOnly = $false; MaxWer = 0.25 },
    @{ Fixture = "sample-tr-cv17-002"; Repeat = 1; DraftPathOnly = $false; MaxWer = 0.4 },
    @{ Fixture = "sample-tr-cv17-001"; Repeat = 5; DraftPathOnly = $true;  MaxWer = 1.0 }
  )
  for ($index = 0; $index -lt $runs.Count; $index++) {
    $fixturesRemaining = $runs.Count - $index
    $remainingSec = $DeadlineSec - $Clock.Elapsed.TotalSeconds
    if ($remainingSec -le (5 * $fixturesRemaining)) { return $false }
    $fixtureBudgetSec = [Math]::Floor($remainingSec / $fixturesRemaining)
    $fixtureDeadlineSec = [Math]::Min(
      $DeadlineSec,
      $Clock.Elapsed.TotalSeconds + $fixtureBudgetSec
    )
    $run = $runs[$index]
    if (-not (Invoke-LiveSttFixtureAcceptance -PythonExe $PythonExe `
        -Clock $Clock -DeadlineSec $fixtureDeadlineSec `
        -FixtureBaseName ([string]$run.Fixture) -Url $Url `
        -ConnectTimeoutCapSec $ConnectTimeoutCapSec `
        -RepeatAudio ([int]$run.Repeat) -DraftPathOnly:([bool]$run.DraftPathOnly) `
        -MaxWordErrorRate ([double]$run.MaxWer))) {
      Write-Host ("[update] fixture acceptance failed: {0} (repeat={1} draftPathOnly={2})" `
        -f $run.Fixture, $run.Repeat, $run.DraftPathOnly) -ForegroundColor Yellow
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
      $env:PLATFORM_AI_TEST_INJECT_ACCEPTANCE_EXCEPTION -eq "1") {
    throw "CI fault injection: acceptance exception"
  }
  if ($script:TestFaultsEnabled -and
      $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE -in @(
        "accept", "reject-twice", "reject-then-accept"
      )) {
    $script:TestAcceptanceInvocation += 1
    if ($env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE -ne "accept" -and (
        $env:PLATFORM_AI_TEST_ACCEPTANCE_SEQUENCE -eq "reject-twice" -or
        $script:TestAcceptanceInvocation -eq 1)) {
      return New-GpuHostAcceptanceResult -Succeeded $false `
        -Reason "injected-acceptance-failure"
    }
    return New-GpuHostAcceptanceResult -Succeeded $true -Reason "accepted"
  }

  $liveSttPythonExe = ""
  $liveSttRuntimeOwner = 0
  $liveSttTaskInstance = $null
  $meetingAiPythonExe = ""
  $meetingAiRuntimeOwner = 0
  $meetingAiTaskInstance = $null
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
    if ($task -eq "platform-ai-meeting-ai") {
      $legacyMeetingAiKeyPath = Join-Path $env:ProgramData `
        "Acik\platform-ai\runtime\meeting-service-client.key"
      if (Test-Path -LiteralPath $legacyMeetingAiKeyPath -PathType Leaf) {
        Remove-Item -LiteralPath $legacyMeetingAiKeyPath -Force
      }
      Remove-MeetingAiStaleRuntimeTlsKeys
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
    } elseif ($task -eq "platform-ai-meeting-ai") {
      $meetingAiPythonExe = $taskPythonExe
      $meetingAiRuntimeOwner = $newOwner
      $meetingAiTaskInstance = $newTaskInstance
    }
    Write-Host "[update] restarted $task with a new task instance and listener" `
      -ForegroundColor Green
  }

  $meetingAiReady = $false
  $meetingAiClock = [Diagnostics.Stopwatch]::StartNew()
  Write-Host "[update] waiting for meeting-ai dependency readiness..." `
    -ForegroundColor Cyan
  while (-not $meetingAiReady -and
      $meetingAiClock.Elapsed.TotalSeconds -lt $script:MeetingAiReadinessDeadlineSec) {
    $remaining = $script:MeetingAiReadinessDeadlineSec - `
      $meetingAiClock.Elapsed.TotalSeconds
    # Per-request budget must exceed the service's measured worst-case /ready
    # latency: the handler runs two synchronous Ollama probes (3s timeout
    # each) on the event loop, so one response can legitimately take ~7-10s
    # under GPU/disk contention. A 5s cap made every poll time out client-side
    # while uvicorn kept logging 200s (Denetim host, 2026-08-02/03 acceptance
    # rejections). The overall MeetingAiReadinessDeadlineSec still bounds the
    # wait; this only sizes each attempt.
    $requestTimeout = [Math]::Max(1, [Math]::Min(30, [Math]::Ceiling($remaining)))
    try {
      $readiness = Invoke-RestMethod "http://127.0.0.1:8300/ready" `
        -TimeoutSec $requestTimeout -ErrorAction Stop
      if (Test-MeetingAiDependencyReadiness -Readiness $readiness) {
        $meetingAiReady = $true
        break
      }
    } catch { }
    Start-Sleep -Milliseconds 1000
  }
  if (-not $meetingAiReady) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "meeting-ai-readiness-failed"
  }
  $meetingTaskPids = @([int]$meetingAiTaskInstance.EnginePid)
  if (-not (Test-GpuHostTaskInstanceStable -TaskName "platform-ai-meeting-ai" `
        -ExpectedInstanceGuid ([string]$meetingAiTaskInstance.InstanceGuid) `
        -ExpectedEnginePid ([int]$meetingAiTaskInstance.EnginePid) `
        -Clock $meetingAiClock -DeadlineSec $script:MeetingAiReadinessDeadlineSec) -or
      -not (Test-GpuHostListenerStable -Port 8300 `
        -ExpectedOwnerId $meetingAiRuntimeOwner -ExpectedPythonExe $meetingAiPythonExe `
        -ExpectedTaskPids $meetingTaskPids -Clock $meetingAiClock `
        -DeadlineSec $script:MeetingAiReadinessDeadlineSec)) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "meeting-ai-readiness-identity-changed"
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
        $readiness.runtime.final.compute_type -eq "float16" -and
        $readiness.speech_gate.profile -eq $script:LiveSttSpeechGateProfile -and
        @("source-baseline", "host-override") -contains $readiness.speech_gate.rms_source -and
        [decimal]$readiness.speech_gate.silence_rms -ge 0.0001 -and
        [decimal]$readiness.speech_gate.silence_rms -le 0.05 -and
        [decimal]$readiness.speech_gate.min_speech_rms -ge
          [decimal]$readiness.speech_gate.silence_rms -and
        [decimal]$readiness.speech_gate.min_speech_rms -le 0.05 -and
        [int]$readiness.speech_gate.live_infer_interval_ms -eq
          $script:LiveSttLiveInferIntervalMs -and
        [decimal]$readiness.speech_gate.live_window_sec -eq
          [decimal]$script:LiveSttLiveWindowSec -and
        [decimal]$readiness.speech_gate.final_window_sec -eq
          [decimal]$script:LiveSttFinalWindowSec -and
        [decimal]$readiness.speech_gate.forced_commit_sec -eq
          [decimal]$script:LiveSttForcedCommitSec -and
        [decimal]$readiness.speech_gate.silence_commit_sec -eq
          [decimal]$script:LiveSttSilenceCommitSec -and
        [decimal]$readiness.speech_gate.tail_overlap_sec -eq
          [decimal]$script:LiveSttTailOverlapSec -and
        [decimal]$readiness.speech_gate.min_infer_sec -eq
          [decimal]$script:LiveSttMinInferSec -and
        $readiness.speech_gate.contextual_artifact.enabled -eq $true -and
        [decimal]$readiness.speech_gate.contextual_artifact.max_rms -eq
          [decimal]$script:LiveSttContextualArtifactMaxRms -and
        [decimal]$readiness.speech_gate.contextual_artifact.min_no_speech_prob -eq
          [decimal]$script:LiveSttContextualArtifactMinNoSpeechProb -and
        $readiness.speech_gate.contextual_artifact.requires_text_match -eq $true -and
        $readiness.speech_gate.vad.live_enabled -eq $true -and
        $readiness.speech_gate.vad.final_enabled -eq $true -and
        $readiness.speech_gate.vad.empty_window_action -eq "skip_decode" -and
        [decimal]$readiness.speech_gate.vad.threshold -eq
          [decimal]$script:LiveSttStreamVadThreshold -and
        [int]$readiness.speech_gate.vad.min_speech_duration_ms -eq
          $script:LiveSttStreamVadMinSpeechDurationMs -and
        [int]$readiness.speech_gate.vad.min_silence_duration_ms -eq
          $script:LiveSttStreamVadMinSilenceDurationMs -and
        [int]$readiness.speech_gate.vad.speech_pad_ms -eq
          $script:LiveSttStreamVadSpeechPadMs
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

function Stop-GpuHostRuntimeFailClosed {
  try {
    $tasks = @(Get-GpuHostManagedTaskNames)
    $ports = @(Get-GpuHostManagedPorts)
    $disabled = Set-GpuHostRuntimeTasksEnabled -Enabled $false
    foreach ($task in $tasks) {
      [void](Invoke-SchtasksTask -Action "/End" -TaskName $task)
    }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt 30) {
      $stopped = $disabled
      foreach ($task in $tasks) {
        if (-not (Test-SchtasksTaskEnabledState -TaskName $task `
            -ExpectedEnabled $false)) {
          $stopped = $false
        }
        $snapshot = Get-GpuHostTaskInstanceSnapshot -TaskName $task
        if (-not $snapshot.Succeeded -or @($snapshot.Instances).Count -gt 0) {
          $stopped = $false
        }
      }
      foreach ($port in $ports) {
        $owners = Get-GpuHostListeningPortOwnerSnapshot -Port $port
        if (-not $owners.Succeeded -or @($owners.Owners).Count -gt 0) {
          $stopped = $false
        }
      }
      if ($stopped) {
        Write-Host "[update] runtime tasks disabled and listeners absent" `
          -ForegroundColor Yellow
        return $true
      }
      Start-Sleep -Milliseconds 500
    }
  } catch {
    Write-Host ("[update] runtime fence exception: {0}" -f `
      $_.Exception.Message) -ForegroundColor Red
  }
  return $false
}

function Invoke-GpuHostTrustedPinFailureRecovery {
  param(
    [Parameter(Mandatory = $true)][string]$RestoreCommit,
    [AllowNull()]$TrustedState,
    [Parameter(Mandatory = $true)][bool]$RequireAcceptance
  )

  $restoreOk = (
    (Invoke-GitStream -GitArgs @(
      "checkout", "--detach", $RestoreCommit
    )) -eq 0 -and
    (Invoke-GitStream -GitArgs @(
      "reset", "--hard", $RestoreCommit
    )) -eq 0
  )
  if (-not $restoreOk) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "source-restore-failed"
  }
  $restoreHead = Invoke-GitCapture -GitArgs @("rev-parse", "HEAD")
  $restoreSymbolic = Invoke-GitCapture -GitArgs @("symbolic-ref", "-q", "HEAD")
  if ($restoreHead.ExitCode -ne 0 -or $restoreHead.Output.Count -ne 1 -or
      "$($restoreHead.Output[0])".Trim().ToLowerInvariant() -ne $RestoreCommit -or
      $restoreSymbolic.ExitCode -eq 0) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "source-restore-postcondition-failed"
  }
  if (-not (Restore-GpuHostTrustedDeploymentState -TrustedState $TrustedState)) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason "ledger-restore-failed"
  }
  if (-not $RequireAcceptance) {
    return New-GpuHostAcceptanceResult -Succeeded $true `
      -Reason "trusted-source-ledger-restored"
  }

  $restoreProfile = "strict-v1"
  if ($RestoreCommit -eq $script:LegacyRollbackCompatCommit) {
    $restoreProfile = "legacy-512e9cc"
  }
  try {
    $acceptance = Invoke-GpuHostRevisionAcceptance `
      -ExpectedCommit $RestoreCommit -AcceptanceProfile $restoreProfile
  } catch {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason ("acceptance-exception-{0}" -f $_.Exception.GetType().Name)
  }
  if (-not $acceptance.Succeeded) {
    return New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason ("runtime-reacceptance-{0}" -f $acceptance.Reason)
  }
  return New-GpuHostAcceptanceResult -Succeeded $true `
    -Reason "trusted-runtime-reaccepted"
}

function Invoke-GpuHostAutomaticRollback {
  param(
    [Parameter(Mandatory = $true)][string]$RestoreCommit,
    [Parameter(Mandatory = $true)][string]$RejectedResult,
    [AllowNull()][string]$RestorePreviousCommit
  )

  $resultWriteFailed = $false
  try {
    Set-DeploymentLedgerResult -Result $RejectedResult
  } catch {
    $resultWriteFailed = $true
    Write-Host ("[update] rejected-result ledger write failed; continuing trusted rollback: {0}" -f `
      $_.Exception.Message) -ForegroundColor Red
  }
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
  try {
    $rollbackAcceptance = Invoke-GpuHostRevisionAcceptance `
      -ExpectedCommit $RestoreCommit -AcceptanceProfile $rollbackProfile
  } catch {
    $rollbackAcceptance = New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason ("acceptance-exception-{0}" -f $_.Exception.GetType().Name)
  }
  if (-not $rollbackAcceptance.Succeeded) {
    try {
      Set-DeploymentLedgerResult `
        -Result ("automatic-rollback-failed-{0}" -f $rollbackAcceptance.Reason)
    } catch {
      Write-Host ("[update] rollback-failure ledger write failed: {0}" -f `
        $_.Exception.Message) -ForegroundColor Red
    }
    return $false
  }
  try {
    Set-DeploymentLedgerResult -Result "automatic-rollback-accepted"
  } catch {
    Write-Host ("[update] rollback-accepted ledger write failed: {0}" -f `
      $_.Exception.Message) -ForegroundColor Red
    return $false
  }
  if ($resultWriteFailed) { return $false }
  return $true
}

if (-not $RecoverFencedRuntime -and
    (Test-GpuHostRuntimeTaskFencePresent)) {
  Stop-Deploy "Runtime task fence is present; recovery requires -RecoverFencedRuntime." `
    $script:DeployExitGuard
}

Invoke-GpuHostSourceAndLedgerMutation

if ($NoRestart) {
  Write-Host "[update] -NoRestart: skipping task restart." -ForegroundColor Yellow
  try {
    Set-DeploymentLedgerResult -Result "pinned-no-restart"
  } catch {
    $fenced = Stop-GpuHostRuntimeFailClosed
    $fenceResult = if ($fenced) { "runtime fenced" } else {
      "runtime fence could not be proven"
    }
    Stop-Deploy ("Pinned source result could not be persisted; {0}: {1}" -f `
      $fenceResult, $_.Exception.Message) $script:DeployExitRollbackFailed
  }
} else {
  if ($RecoverFencedRuntime -and
      -not (Set-GpuHostRuntimeTasksEnabled -Enabled $true)) {
    [void](Stop-GpuHostRuntimeFailClosed)
    Stop-Deploy "Explicit fenced-runtime recovery could not enable and verify both tasks." `
      $script:DeployExitRollbackFailed
  }
  $targetAcceptanceProfile = "strict-v1"
  if ($Rollback -and $target -eq $script:LegacyRollbackCompatCommit) {
    $targetAcceptanceProfile = "legacy-512e9cc"
  }
  try {
    $acceptance = Invoke-GpuHostRevisionAcceptance -ExpectedCommit $target `
      -AcceptanceProfile $targetAcceptanceProfile
  } catch {
    $acceptance = New-GpuHostAcceptanceResult -Succeeded $false `
      -Reason ("acceptance-exception-{0}" -f $_.Exception.GetType().Name)
  }
  if (-not $acceptance.Succeeded) {
    $restoreCommit = $before
    $restorePreviousCommit = $null
    if ($state) {
      $restorePreviousCommit = $state.previousCommit
      if ($ReconcileLedgerDrift) { $restoreCommit = $state.currentCommit }
    }
    if (-not (Invoke-GpuHostAutomaticRollback -RestoreCommit $restoreCommit `
        -RejectedResult $acceptance.Reason `
        -RestorePreviousCommit $restorePreviousCommit)) {
      $fenced = Stop-GpuHostRuntimeFailClosed
      $fenceResult = if ($fenced) { "runtime fenced" } else {
        "runtime fence could not be proven"
      }
      Stop-Deploy ("Revision acceptance and automatic rollback acceptance failed; {0}." -f `
        $fenceResult) `
        $script:DeployExitRollbackFailed
    }
    Stop-Deploy "Revision acceptance failed; previous revision was restored and reaccepted." `
      $script:DeployExitRestartFailed
  }
}

Write-Host "[update] done. Verify: Invoke-RestMethod http://127.0.0.1:8200/health ; :8200/ready ; :8300/health ; ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1 ready" -ForegroundColor Cyan
if (-not $NoRestart) {
  try {
    Set-DeploymentLedgerResult -Result "tasks-restarted"
  } catch {
    $fenced = Stop-GpuHostRuntimeFailClosed
    $fenceResult = if ($fenced) { "runtime fenced" } else {
      "runtime fence could not be proven"
    }
    Stop-Deploy ("Accepted runtime result could not be persisted; {0}: {1}" -f `
      $fenceResult, $_.Exception.Message) $script:DeployExitRollbackFailed
  }
}
if ($script:DeployMutex) {
  if ($script:DeployLockTaken) { $script:DeployMutex.ReleaseMutex() }
  $script:DeployMutex.Dispose()
  $script:DeployMutex = $null
}
exit 0
