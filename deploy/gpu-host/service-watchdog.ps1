<#
.SYNOPSIS
  Restart GPU-host service tasks that are down (gitops#3888).

.DESCRIPTION
  Task Scheduler only retries a task whose *launch* fails; a launcher that
  exits non-zero (meeting-ai refusing to start without Ollama) or a service
  process that dies later is never restarted. On 2026-10-14 the host booted
  before any Ollama listened on :11434, meeting-ai's bounded readiness wait
  expired, the task exited 0x1 and :8300 stayed down for 8 days.

  This watchdog runs every few minutes and, for each managed task, starts it
  with schtasks /Run only when ALL of these hold:
    - no deploy is in progress (Global\platform-ai-gpu-deploy-v1 does not
      exist; checked with TryOpenExisting, never acquired, so it can not
      make a deploy fail to take its lock),
    - the task is enabled (a fenced runtime has its tasks disabled on
      purpose and stays down),
    - the task has no running instance,
    - nothing listens on the task's port.
  Otherwise it does nothing. An optional dependency task (the Ollama server
  meeting-ai needs) is supervised the same way and started first.

  Exit 0 = every managed service up or legitimately left alone;
  exit 3 = at least one service was down (and a start was attempted).
#>
[CmdletBinding()]
param(
  [string]$LogDir = "",
  [string]$DependencyTaskName = "",
  [int]$DependencyPort = 11434,
  [switch]$ProbeOnly
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "task-action-contract.ps1")
. (Join-Path $PSScriptRoot "restart-acceptance.ps1")

$script:DeployMutexName = "Global\platform-ai-gpu-deploy-v1"
$script:ManagedServices = @(
  [pscustomobject]@{ TaskName = "platform-ai-live-stt"; Port = 8200 },
  [pscustomobject]@{ TaskName = "platform-ai-meeting-ai"; Port = 8300 }
)

if ([string]::IsNullOrWhiteSpace($LogDir)) { $LogDir = Join-Path $PSScriptRoot "logs" }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$script:Log = Join-Path $LogDir ("service-watchdog-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-WatchdogLog([string]$Message) {
  Add-Content $script:Log ("{0} {1}" -f (Get-Date -Format "s"), $Message)
}

function Test-GpuHostDeployInProgress {
  $existing = $null
  if ([Threading.Mutex]::TryOpenExisting($script:DeployMutexName, [ref]$existing)) {
    $existing.Dispose()
    return $true
  }
  return $false
}

function Get-WatchdogTaskState {
  param([Parameter(Mandatory = $true)][string]$TaskName)
  try {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $task = $service.GetFolder("\").GetTask($TaskName)
    return [pscustomobject]@{
      Exists = $true
      Enabled = [bool]$task.Enabled
      Running = (@($task.GetInstances(0)).Count -gt 0)
    }
  } catch {
    return [pscustomobject]@{ Exists = $false; Enabled = $false; Running = $false }
  }
}

function Invoke-WatchdogService {
  param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][int]$Port
  )
  $state = Get-WatchdogTaskState -TaskName $TaskName
  if (-not $state.Exists) { Write-WatchdogLog "skip ${TaskName}: task not installed"; return "absent" }
  if (-not $state.Enabled) { Write-WatchdogLog "skip ${TaskName}: task disabled (fenced)"; return "fenced" }
  $owners = Get-GpuHostListeningPortOwnerSnapshot -Port $Port
  if ($state.Running -or ($owners.Succeeded -and @($owners.Owners).Count -gt 0)) { return "up" }
  if ($ProbeOnly) { Write-WatchdogLog "down $TaskName (port $Port): probe-only, not starting"; return "down" }
  $exit = Invoke-SchtasksRun -TaskName $TaskName
  Write-WatchdogLog "down $TaskName (port $Port): schtasks /Run exit=$exit"
  return "restarted"
}

function Invoke-SchtasksRun {
  param([Parameter(Mandatory = $true)][string]$TaskName)
  $oldEap = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & schtasks.exe /Run /TN $TaskName 1> $null 2> $null
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldEap
  }
}

if (Test-GpuHostDeployInProgress) {
  Write-WatchdogLog "skip all: deploy in progress ($script:DeployMutexName held)"
  exit 0
}

$down = $false
if (-not [string]::IsNullOrWhiteSpace($DependencyTaskName)) {
  $dep = Invoke-WatchdogService -TaskName $DependencyTaskName -Port $DependencyPort
  if ($dep -in @("down", "restarted")) { $down = $true }
}
foreach ($svc in $script:ManagedServices) {
  $r = Invoke-WatchdogService -TaskName $svc.TaskName -Port $svc.Port
  if ($r -in @("down", "restarted")) { $down = $true }
}
if ($down) { exit 3 }
exit 0
