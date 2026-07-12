<#
.SYNOPSIS
  Immutable deployment-ledger drift guard for the Windows GPU host.

.DESCRIPTION
  Refreshes origin refs, reads the hardened deployment-state.json, and proves
  the deploy clone is detached at ledger.currentCommit. A pinned commit being
  behind a newer origin/main is expected and is NOT drift. Drift is instead:
  malformed/insecure ledger, HEAD != expected commit, symbolic branch HEAD,
  dirty tracked files, missing commit object, or expected commit no longer
  being an ancestor of origin/main.

  Exit 0 means the bounded source checks passed. Exit 2 means drift or stale
  remote truth. The script never changes HEAD or tracked working-tree files.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = "",
  [string]$Branch    = "main",
  [string]$StatePath = "C:\ProgramData\Acik\platform-ai\deployment-state.json",
  [string]$LogDir    = ""
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

if ([string]::IsNullOrWhiteSpace($RepoRoot) -or
    [string]::IsNullOrWhiteSpace($LogDir)) {
  $scriptDir = $PSScriptRoot
  if ([string]::IsNullOrWhiteSpace($scriptDir) -and $PSCommandPath) {
    $scriptDir = Split-Path -Parent $PSCommandPath
  }
  if ([string]::IsNullOrWhiteSpace($scriptDir) -and
      $MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  }
  if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    Write-Error "Could not resolve script directory."
    exit 2
  }
  if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
  }
  if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $scriptDir "logs"
  }
}

if ($Branch -notmatch '^[A-Za-z0-9._/-]+$') {
  Write-Error "Branch contains unsupported characters."
  exit 2
}
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
  Write-Error "RepoRoot is not a git clone."
  exit 2
}

$stateModule = Join-Path $PSScriptRoot "deployment-state.ps1"
if (-not (Test-Path -LiteralPath $stateModule -PathType Leaf)) {
  Write-Error "Missing deployment-state.ps1 beside drift-guard.ps1."
  exit 2
}
. $stateModule

Set-Location $RepoRoot
New-Item -ItemType Directory -Force $LogDir | Out-Null
$log = Join-Path $LogDir ("drift-guard-{0:yyyyMMdd}.log" -f (Get-Date))
function Note($message) {
  ("[{0:u}] {1}" -f (Get-Date), $message) |
    Tee-Object -FilePath $log -Append
}

$originRef = "origin/{0}" -f $Branch
$originRemoteRef = "refs/remotes/{0}" -f $originRef

git fetch --prune origin 1> $null 2> $null
if ($LASTEXITCODE -ne 0) {
  Note "DRIFT: git fetch failed; remote truth is stale."
  exit 2
}
git rev-parse --verify --quiet $originRemoteRef *> $null
if ($LASTEXITCODE -ne 0) {
  Note "DRIFT: $originRef is unavailable after fetch."
  exit 2
}

try {
  $state = Read-DeploymentState -StatePath $StatePath
} catch {
  Note ("DRIFT: deployment ledger rejected: {0}" -f $_.Exception.Message)
  exit 2
}
if (-not $state) {
  Note "DRIFT: deployment ledger is missing."
  exit 2
}
if ($state.branchRef -ne $originRemoteRef) {
  Note "DRIFT: ledger branchRef does not match the configured origin ref."
  exit 2
}
$expected = $state.currentCommit

$objectSpec = $expected + "^{commit}"
$object = @(git rev-parse --verify $objectSpec 2> $null)
if ($LASTEXITCODE -ne 0 -or $object.Count -ne 1 -or
    "$($object[0])".Trim().ToLowerInvariant() -ne $expected) {
  Note "DRIFT: ledger currentCommit is not an available exact commit object."
  exit 2
}
git merge-base --is-ancestor $expected $originRef 1> $null 2> $null
if ($LASTEXITCODE -ne 0) {
  Note "DRIFT: ledger currentCommit is no longer an ancestor of $originRef."
  exit 2
}

$actual = @(git rev-parse HEAD 2> $null)
if ($LASTEXITCODE -ne 0 -or $actual.Count -ne 1) {
  Note "DRIFT: git rev-parse HEAD failed."
  exit 2
}
$actualCommit = "$($actual[0])".Trim().ToLowerInvariant()
git symbolic-ref -q HEAD 1> $null 2> $null
$isSymbolic = ($LASTEXITCODE -eq 0)
$dirty = @(git status --porcelain --untracked-files=no 2> $null)
if ($LASTEXITCODE -ne 0) {
  Note "DRIFT: git status failed."
  exit 2
}

$alerts = @()
if ($actualCommit -ne $expected) {
  $alerts += "HEAD does not match ledger currentCommit"
}
if ($isSymbolic) {
  $alerts += "HEAD is symbolic; immutable deploy must be detached"
}
if ($dirty.Count -gt 0) {
  $alerts += "$($dirty.Count) modified tracked file(s)"
}

if ($alerts.Count -eq 0) {
  Note "OK: detached immutable HEAD matches the hardened ledger and remains in $originRef ancestry."
  exit 0
}
foreach ($alert in $alerts) { Note "DRIFT: $alert" }

$mavis = Get-Command mavis -ErrorAction SilentlyContinue
if ($mavis -and $env:MAVIS_PEER) {
  & mavis communication send --to $env:MAVIS_PEER --command prompt `
    --content ("denetim-PC immutable deploy drift: " + ($alerts -join " | ")) `
    1> $null 2> $null
}
exit 2
