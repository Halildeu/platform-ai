<#
.SYNOPSIS
  Read-only drift + un-backed-up-work guard for the GPU-host deploy clone.

.DESCRIPTION
  Run daily (Scheduled Task). NEVER mutates the repo. Warns to a daily log and,
  if MAVIS_PEER is set, sends a redacted (no-secret) Mavis ping.

  Catches the 2026-06-21 failure mode early: the deploy clone accumulating
  unpushed local commits (#161/#164 sat local-only = single point of failure)
  or drifting off main. Pair with update.ps1 (the drift-proof updater).

.PARAMETER RepoRoot
  Deploy clone path. Defaults to the repo this script lives in (deploy/gpu-host/..).

.PARAMETER Branch
  Expected tracking branch. Default 'main'.

.PARAMETER LogDir
  Where to write the daily log. Default deploy/gpu-host/logs.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
  [string]$Branch   = "main",
  [string]$LogDir   = (Join-Path $PSScriptRoot "logs")
)
$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"
Set-Location $RepoRoot
New-Item -ItemType Directory -Force $LogDir | Out-Null
$log = Join-Path $LogDir ("drift-guard-{0:yyyyMMdd}.log" -f (Get-Date))
function Note($m){ ("[{0:u}] {1}" -f (Get-Date), $m) | Tee-Object -FilePath $log -Append }

git fetch --prune origin 2>&1 | Out-Null
$head     = (git rev-parse --abbrev-ref HEAD).Trim()
$unpushed = @(git log --oneline "origin/$Branch..HEAD" 2>$null)
$behind   = @(git log --oneline "HEAD..origin/$Branch" 2>$null)
$dirty    = @(git status --porcelain --untracked-files=no)

$alerts = @()
if ($head -ne $Branch)     { $alerts += "HEAD '$head' != '$Branch' (deploy clone must track $Branch)" }
if ($unpushed.Count -gt 0) { $alerts += "$($unpushed.Count) UNPUSHED local commit(s) — SPOF, push + PR now" }
if ($dirty.Count -gt 0)    { $alerts += "$($dirty.Count) modified tracked file(s) — clone is not clean" }
if ($behind.Count -gt 0)   { $alerts += "$($behind.Count) commit(s) behind origin/$Branch — run update.ps1" }

if ($alerts.Count -eq 0) { Note "OK: clean, on $Branch, == origin/$Branch."; exit 0 }
foreach ($a in $alerts) { Note "DRIFT: $a" }

# Redacted (no-secret) Mavis ping, if configured.
$mavis = Get-Command mavis -ErrorAction SilentlyContinue
if ($mavis -and $env:MAVIS_PEER) {
  & mavis communication send --to $env:MAVIS_PEER --command prompt --content ("denetim-PC deploy-clone drift: " + ($alerts -join " | ")) 2>&1 | Out-Null
}
exit 2
