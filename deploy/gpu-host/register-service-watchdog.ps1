<#
.SYNOPSIS
  Register the GPU-host service watchdog task (gitops#3888).

.DESCRIPTION
  Creates/updates SYSTEM task "platform-ai-service-watchdog" that runs
  service-watchdog.ps1 three minutes after boot and every five minutes after
  that. MultipleInstances=IgnoreNew keeps runs from overlapping; the watchdog
  itself never acts while a deploy holds Global\platform-ai-gpu-deploy-v1.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = "",
  [string]$DependencyTaskName = "Denetim-Ollama-11434",
  [int]$IntervalMinutes = 5
)
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
  $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$script = Join-Path $RepoRoot "deploy\gpu-host\service-watchdog.ps1"
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Missing $script" }

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
if (-not [string]::IsNullOrWhiteSpace($DependencyTaskName)) {
  $arguments += " -DependencyTaskName `"$DependencyTaskName`""
}
$action = New-ScheduledTaskAction -Execute $ps -Argument $arguments

$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = "PT3M"
$boot.Repetition = $repeat.Repetition

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "platform-ai-service-watchdog" -Action $action `
  -Trigger @($boot, $repeat) -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered platform-ai-service-watchdog (every $IntervalMinutes min, dependency '$DependencyTaskName')."
