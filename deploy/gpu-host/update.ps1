<#
.SYNOPSIS
  Drift-proof GPU-host deploy update. Pins the deploy clone's tracked working
  tree to origin/main, then restarts the live-stt + meeting-ai scheduled tasks.

.DESCRIPTION
  Replaces the fragile `cd C:\platform-ai; git pull` in README §Güncelleme.

  The deploy clone (e.g. C:\Users\denetimpc\platform-ai) is a READ-ONLY MIRROR
  of origin/main. Development must NEVER happen here — see drift-guard.ps1 and
  the 2026-06-21 incident where 13 unpushed diarization commits (#161/#164) sat
  local-only on this clone (single point of failure, no GitHub backup).

  FAIL-CLOSED SAFETY: this script REFUSES to `git reset --hard` if it would
  destroy un-backed-up local work (commits not in origin/$Branch, or a dirty
  tracked tree) OR if it cannot positively verify the state (missing origin ref,
  a failed git query). On any doubt it aborts and tells the operator to push +
  PR first. -Force overrides ONLY after the work is confirmed on GitHub.

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

# 1. Refresh remote refs (needed to compare against origin/$Branch). Fail-closed.
git fetch --prune origin 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "git fetch failed (network / auth). Aborting — no mutation." }

# 2. FAIL-CLOSED guard. EVERY safety query must succeed; if we cannot positively
#    verify the state we ABORT, rather than risk a reset --hard that loses work.
git rev-parse --verify --quiet "refs/remotes/origin/$Branch" *> $null
if ($LASTEXITCODE -ne 0) { Fail "origin/$Branch not found after fetch — cannot verify safety. Aborting." }

$head = git rev-parse --abbrev-ref HEAD
if ($LASTEXITCODE -ne 0) { Fail "git rev-parse HEAD failed — cannot verify safety. Aborting." }
$head = "$head".Trim()

$dirty = @(git status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) { Fail "git status failed — cannot verify safety. Aborting." }

$unpushed = @(git log --oneline "origin/$Branch..HEAD")
if ($LASTEXITCODE -ne 0) { Fail "git log origin/$Branch..HEAD failed — cannot verify local work. Aborting." }

if (($unpushed.Count -gt 0 -or $dirty.Count -gt 0) -and -not $Force) {
  Write-Host ""
  Write-Host "  ABORT — un-backed-up local work would be DESTROYED by reset --hard:" -ForegroundColor Red
  if ($head -ne $Branch)     { Write-Host "    - HEAD is on '$head', not '$Branch'" -ForegroundColor Yellow }
  if ($unpushed.Count -gt 0) { Write-Host "    - $($unpushed.Count) local commit(s) not in origin/${Branch}:" -ForegroundColor Yellow; $unpushed | ForEach-Object { Write-Host "        $_" } }
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

# 3. Pin to origin/$Branch atomically (checkout -B sets the branch + tree), then
#    reset --hard (belt-and-suspenders). Each step is exit-checked — a failed
#    checkout must NOT fall through to reset on the wrong ref.
$before = (git rev-parse HEAD).Trim()
# -Force genuinely discards confirmed-preserved local work (clobbers a dirty
# tracked tree); the non-Force path stays safe and aborts on any obstruction.
if ($Force) {
  git checkout -f -B $Branch "origin/$Branch" 2>&1 | Out-Host
} else {
  git checkout -B $Branch "origin/$Branch" 2>&1 | Out-Host
}
if ($LASTEXITCODE -ne 0) { Fail "git checkout -B $Branch origin/$Branch failed — deploy state unchanged." }
git reset --hard "origin/$Branch" 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "git reset --hard origin/$Branch failed." }
$after = (git rev-parse HEAD).Trim()
Write-Host "[update] $before -> $after (tracked tree pinned to origin/$Branch)" -ForegroundColor Green

# 4. Restart the deploy scheduled tasks so they pick up the new code. Use the
#    always-present schtasks.exe rather than the *-ScheduledTask cmdlets: the
#    ScheduledTasks module is ABSENT on some hosts (this GPU host's Windows
#    PowerShell 5.1 has no Restart-ScheduledTask — Get-ScheduledTask would throw
#    CommandNotFound and, under $ErrorActionPreference=Stop, abort the whole
#    update after the git pin already landed). #193 follow-up; Codex review #194.
#
#    CRITICAL: schtasks writes benign stderr on /Query-missing and /End-not-
#    running. PowerShell's `2>&1` pipe can wrap native stderr into a
#    NativeCommandError that, under $ErrorActionPreference=Stop, terminates BEFORE
#    the $LASTEXITCODE check — re-introducing the same "git pin landed, restart
#    aborted" failure. So route every call through a helper that drops stderr
#    WITHOUT the PS 2>&1 pipe and forces EAP=Continue around the native call.
function Invoke-SchtasksQuiet {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$SchtasksArgs)
  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & schtasks.exe @SchtasksArgs 1> $null 2> $null
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

if ($NoRestart) {
  Write-Host "[update] -NoRestart: skipping task restart." -ForegroundColor Yellow
} else {
  $restartFailed = $false
  foreach ($task in @("platform-ai-live-stt", "platform-ai-meeting-ai")) {
    if ((Invoke-SchtasksQuiet "/Query" "/TN" $task) -ne 0) {
      Write-Host "[update] task '$task' not installed (skipping)" -ForegroundColor Yellow
      continue
    }
    # /End returns non-zero when the task is not running (benign). When it WAS
    # running, give the process ~2s to release its listening port before /Run
    # starts a fresh instance (live-stt/meeting-ai bind 8200/8300).
    if ((Invoke-SchtasksQuiet "/End" "/TN" $task) -eq 0) { Start-Sleep -Seconds 2 }
    $runExit = Invoke-SchtasksQuiet "/Run" "/TN" $task
    if ($runExit -ne 0) {
      Write-Host "[update] ERROR: schtasks /Run '$task' exit=$runExit" -ForegroundColor Red
      $restartFailed = $true
    } else {
      Write-Host "[update] restarted $task" -ForegroundColor Green
    }
  }
  if ($restartFailed) { Fail "One or more scheduled tasks failed to restart (git pin landed; services may be stale code)." }
}

Write-Host "[update] done. Verify: Invoke-RestMethod http://127.0.0.1:8200/health ; :8300/health" -ForegroundColor Cyan
