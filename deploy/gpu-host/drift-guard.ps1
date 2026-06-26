<#
.SYNOPSIS
  Drift + un-backed-up-work guard for the GPU-host deploy clone.

.DESCRIPTION
  Run daily (Scheduled Task). Does NOT modify HEAD or tracked working-tree files.
  It DOES refresh remote-tracking refs (git fetch --prune) and write a daily log
  under deploy/gpu-host/logs - those are the only side effects.

  Warns (log + optional redacted Mavis ping) on: HEAD!=main / commits not in
  origin / dirty tree / behind origin. FAIL-CLOSED: a failed fetch (or a missing
  origin ref) is itself treated as drift (exit 2), never a silent OK - otherwise
  a daily guard could pass on stale remote truth.

  Catches the 2026-06-21 failure mode early: the deploy clone accumulating local
  commits (#161/#164 sat local-only = single point of failure) or drifting off
  main. Pair with update.ps1 (the drift-proof updater).

.PARAMETER RepoRoot
  Deploy clone path. Defaults to the repo this script lives in (deploy/gpu-host/..).

.PARAMETER Branch
  Expected tracking branch. Default 'main'.

.PARAMETER LogDir
  Where to write the daily log. Default deploy/gpu-host/logs.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = "",
  [string]$Branch   = "main",
  [string]$LogDir   = ""
)
$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"

if ([string]::IsNullOrWhiteSpace($RepoRoot) -or [string]::IsNullOrWhiteSpace($LogDir)) {
  $scriptDir = $PSScriptRoot
  if ([string]::IsNullOrWhiteSpace($scriptDir) -and $PSCommandPath) {
    $scriptDir = Split-Path -Parent $PSCommandPath
  }
  if ([string]::IsNullOrWhiteSpace($scriptDir) -and $MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  }
  if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    Write-Error "Could not resolve script directory. Pass -RepoRoot and -LogDir explicitly."
    exit 2
  }
  if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
  }
  if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $scriptDir "logs"
  }
}

Set-Location $RepoRoot
New-Item -ItemType Directory -Force $LogDir | Out-Null
$log = Join-Path $LogDir ("drift-guard-{0:yyyyMMdd}.log" -f (Get-Date))
function Note($m){ ("[{0:u}] {1}" -f (Get-Date), $m) | Tee-Object -FilePath $log -Append }

$originRef = "origin/{0}" -f $Branch
$originRemoteRef = "refs/remotes/{0}" -f $originRef
$unpushedRange = "{0}..HEAD" -f $originRef
$behindRange = "HEAD..{0}" -f $originRef

# Fail-closed: stale/failed remote truth is drift, not OK.
git fetch --prune origin 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Note "DRIFT: git fetch failed - remote truth stale (network/auth?). Treating as drift."; exit 2 }

git rev-parse --verify --quiet $originRemoteRef *> $null
if ($LASTEXITCODE -ne 0) { Note "DRIFT: $originRef not found - cannot compare against deploy baseline."; exit 2 }

$head = "$(git rev-parse --abbrev-ref HEAD)".Trim()
if ($LASTEXITCODE -ne 0) { Note "DRIFT: git rev-parse HEAD failed."; exit 2 }
$unpushed = @(git log --oneline $unpushedRange)
if ($LASTEXITCODE -ne 0) { Note "DRIFT: git log $unpushedRange failed."; exit 2 }
$behind = @(git log --oneline $behindRange)
if ($LASTEXITCODE -ne 0) { Note "DRIFT: git log $behindRange failed."; exit 2 }
$dirty = @(git status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) { Note "DRIFT: git status failed."; exit 2 }

$alerts = @()
if ($head -ne $Branch)     { $alerts += "HEAD '$head' != '$Branch' (deploy clone must track $Branch)" }
if ($unpushed.Count -gt 0) { $alerts += "$($unpushed.Count) local commit(s) not in $originRef - SPOF, push + PR now" }
if ($dirty.Count -gt 0)    { $alerts += "$($dirty.Count) modified tracked file(s) - clone is not clean" }
if ($behind.Count -gt 0)   { $alerts += "$($behind.Count) commit(s) behind $originRef - run update.ps1" }

if ($alerts.Count -eq 0) { Note "OK: clean, on $Branch, == $originRef."; exit 0 }
foreach ($a in $alerts) { Note "DRIFT: $a" }

# Redacted (no-secret) Mavis ping, if configured.
$mavis = Get-Command mavis -ErrorAction SilentlyContinue
if ($mavis -and $env:MAVIS_PEER) {
  & mavis communication send --to $env:MAVIS_PEER --command prompt --content ("denetim-PC deploy-clone drift: " + ($alerts -join " | ")) 2>&1 | Out-Null
}
exit 2
