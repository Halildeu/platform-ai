<#
.SYNOPSIS
  Drift-proof GPU-host deploy update. Forces the deploy clone to EXACTLY match
  origin/main, then restarts the live-stt + meeting-ai scheduled tasks.

.DESCRIPTION
  Replaces the fragile `cd C:\platform-ai; git pull` in README §Güncelleme.

  The deploy clone (e.g. C:\Users\denetimpc\platform-ai) is a READ-ONLY MIRROR
  of origin/main. Development must NEVER happen here — see drift-guard.ps1 and
  the 2026-06-21 incident where 13 unpushed diarization commits (#161/#164) sat
  local-only on this clone (single point of failure, no GitHub backup).

  FAIL-CLOSED SAFETY: this script REFUSES to run `git reset --hard` if it would
  destroy un-backed-up local work (unpushed commits or a dirty tracked tree).
  In that case it aborts and tells the operator to push + PR first. -Force
  overrides ONLY after the operator has confirmed the work is preserved.

.PARAMETER RepoRoot
  Deploy clone path. Defaults to the repo this script lives in (deploy/gpu-host/..).

.PARAMETER Branch
  Tracking branch. Default 'main'. The deploy clone tracks main only.

.PARAMETER NoRestart
  Update the working tree but do not restart the scheduled tasks.

.PARAMETER Force
  Override the unpushed-work safety abort. Use ONLY after preserving local work.

.EXAMPLE
  cd C:\Users\denetimpc\platform-ai
  Set-ExecutionPolicy -Scope Process Bypass
  .\deploy\gpu-host\update.ps1
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
  [string]$Branch   = "main",
  [switch]$NoRestart,
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
function Fail($msg) { Write-Error $msg; exit 1 }

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
  Fail "RepoRoot '$RepoRoot' is not a git clone. Pass -RepoRoot explicitly."
}
Set-Location $RepoRoot
Write-Host "[update] repo=$RepoRoot branch=$Branch" -ForegroundColor Cyan

# 1. Refresh remote refs (read-only; needed to compare against origin/$Branch).
git fetch --prune origin 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "git fetch failed (network / auth). Aborting — no mutation." }

# 2. FAIL-CLOSED guard — never destroy un-backed-up local work.
$head     = (git rev-parse --abbrev-ref HEAD).Trim()
$dirty    = @(git status --porcelain --untracked-files=no)
$unpushed = @(git log --oneline "origin/$Branch..HEAD" 2>$null)

if (($unpushed.Count -gt 0 -or $dirty.Count -gt 0) -and -not $Force) {
  Write-Host ""
  Write-Host "  ABORT — un-backed-up local work would be DESTROYED by reset --hard:" -ForegroundColor Red
  if ($head -ne $Branch)     { Write-Host "    - HEAD is on '$head', not '$Branch'" -ForegroundColor Yellow }
  if ($unpushed.Count -gt 0) { Write-Host "    - $($unpushed.Count) unpushed local commit(s):" -ForegroundColor Yellow; $unpushed | ForEach-Object { Write-Host "        $_" } }
  if ($dirty.Count -gt 0)    { Write-Host "    - $($dirty.Count) modified tracked file(s)" -ForegroundColor Yellow }
  Write-Host ""
  Write-Host "  This clone is a deploy MIRROR — it must not hold local work." -ForegroundColor Red
  Write-Host "  Preserve it FIRST, then re-run:" -ForegroundColor Red
  Write-Host "    1. In your DEV clone (not here): push the branch + open a PR." -ForegroundColor Gray
  Write-Host "    2. If the work only exists here: extract via git bundle ->" -ForegroundColor Gray
  Write-Host "       git bundle create C:\Temp\save.bundle $head --not origin/$Branch" -ForegroundColor Gray
  Write-Host "       then copy it off-box and push from a credentialed clone." -ForegroundColor Gray
  Write-Host "    3. Only after it is on GitHub: re-run with -Force." -ForegroundColor Gray
  Fail "Refusing to discard un-backed-up work without -Force."
}

# 3. Pin the deploy clone to origin/$Branch — drift becomes impossible.
$before = (git rev-parse HEAD).Trim()
git checkout $Branch 2>&1 | Out-Host
git reset --hard "origin/$Branch" 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "git reset --hard origin/$Branch failed." }
$after = (git rev-parse HEAD).Trim()
Write-Host "[update] $before -> $after (now == origin/$Branch)" -ForegroundColor Green

# 4. Restart the deploy scheduled tasks so they pick up the new code.
if ($NoRestart) {
  Write-Host "[update] -NoRestart: skipping task restart." -ForegroundColor Yellow
} else {
  foreach ($task in @("platform-ai-live-stt", "platform-ai-meeting-ai")) {
    $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "[update] task '$task' not installed (skipping)" -ForegroundColor Yellow; continue }
    try { Restart-ScheduledTask -TaskName $task -ErrorAction Stop }
    catch { Stop-ScheduledTask -TaskName $task -EA SilentlyContinue; Start-ScheduledTask -TaskName $task }
    Write-Host "[update] restarted $task" -ForegroundColor Green
  }
}

Write-Host "[update] done. Verify: Invoke-RestMethod http://127.0.0.1:8200/health ; :8300/health" -ForegroundColor Cyan
