<#
.SYNOPSIS
  Preserves untracked GPU-host deploy-mirror files in a restricted,
  content-addressed quarantine before immutable source reconciliation.

.DESCRIPTION
  The deploy clone is a read-only mirror. This script enumerates untracked
  non-ignored files, validates that each item is a regular file below RepoRoot,
  hashes and copies it into a SYSTEM + Administrators-only quarantine, writes
  and verifies a restricted manifest, then removes the verified source copies.

  Console output is status-only: count, aggregate bytes, receipt path, and
  manifest digest. Source names and content are never written to console.

.PARAMETER RepoRoot
  GPU-host platform-ai deploy mirror.

.PARAMETER QuarantineRoot
  Fixed-local restricted root outside RepoRoot.

.PARAMETER ExpectedCount
  Optional exact untracked-file count from a stable preflight snapshot.

.EXAMPLE
  .\preserve-untracked.ps1 -RepoRoot C:\platform-ai -ExpectedCount 79 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
  [Parameter(Mandatory = $true)][string]$RepoRoot,
  [string]$QuarantineRoot = `
    "C:\ProgramData\Acik\platform-ai\untracked-quarantine",
  [int]$ExpectedCount = -1
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Stop-Preserve {
  param([Parameter(Mandatory = $true)][string]$Message)
  [Console]::Error.WriteLine("[preserve-untracked] ERROR: {0}" -f $Message)
  exit 2
}

function Get-Sha256 {
  param([Parameter(Mandatory = $true)][string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-FixedLocalPath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Purpose
  )

  $full = [IO.Path]::GetFullPath($Path)
  if (-not [IO.Path]::IsPathRooted($full) -or
      $full.StartsWith("\\", [StringComparison]::Ordinal) -or
      $full.StartsWith("\\?\", [StringComparison]::Ordinal) -or
      $full.StartsWith("\\.\", [StringComparison]::Ordinal)) {
    throw "$Purpose must be an absolute fixed-local path."
  }
  $root = [IO.Path]::GetPathRoot($full)
  $drive = New-Object IO.DriveInfo($root)
  if ($drive.DriveType -ne [IO.DriveType]::Fixed) {
    throw "$Purpose must reside on a fixed local volume."
  }
  return $full.TrimEnd('\')
}

function Invoke-GitUntrackedNul {
  param([Parameter(Mandatory = $true)][string]$WorkingDirectory)

  if ($WorkingDirectory.Contains('"') -or
      $WorkingDirectory.Contains("`r") -or
      $WorkingDirectory.Contains("`n")) {
    throw "RepoRoot contains unsupported characters."
  }
  $start = New-Object Diagnostics.ProcessStartInfo
  $start.FileName = "git.exe"
  $start.Arguments = ('-C "{0}" ls-files --others --exclude-standard -z' -f `
    $WorkingDirectory)
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $start
  [void]$process.Start()
  $stdout = $process.StandardOutput.ReadToEnd()
  $stderr = $process.StandardError.ReadToEnd()
  $process.WaitForExit()
  if ($process.ExitCode -ne 0) {
    throw "git untracked-content scan failed."
  }
  if (-not [string]::IsNullOrWhiteSpace($stderr)) {
    throw "git untracked-content scan emitted unexpected diagnostics."
  }
  if ([string]::IsNullOrEmpty($stdout)) { return @() }
  return @($stdout.Split([char]0) | Where-Object {
    -not [string]::IsNullOrEmpty($_)
  })
}

function Assert-NoReparsePoint {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$StopAt,
    [string]$Purpose = "Path"
  )

  $cursor = [IO.Path]::GetFullPath($Path)
  $root = [IO.Path]::GetFullPath($StopAt)
  if (-not $root.Equals(
      [IO.Path]::GetPathRoot($root),
      [StringComparison]::OrdinalIgnoreCase
    )) {
    $root = $root.TrimEnd('\')
  }
  while ($cursor.Length -ge $root.Length) {
    if (Test-Path -LiteralPath $cursor) {
      $item = Get-Item -LiteralPath $cursor -Force
      if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Purpose must not traverse or use a reparse point."
      }
    }
    if ($cursor.Equals($root, [StringComparison]::OrdinalIgnoreCase)) {
      return
    }
    $parent = Split-Path -Parent $cursor
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
      break
    }
    $cursor = $parent
  }
  throw "$Purpose escaped its approved root."
}

try {
  if ($ExpectedCount -lt -1) {
    throw "ExpectedCount must be -1 or a non-negative integer."
  }
  $RepoRoot = Assert-FixedLocalPath `
    -Path (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path `
    -Purpose "RepoRoot"
  $QuarantineRoot = Assert-FixedLocalPath -Path $QuarantineRoot `
    -Purpose "QuarantineRoot"
  $runnerTemp = ""
  if (-not [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    $runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
  }
  $currentIdentity = ""
  try {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  } catch { }
  $testFaultsEnabled = (
    $env:CI -eq "true" -and
    $env:GITHUB_ACTIONS -eq "true" -and
    $env:RUNNER_ENVIRONMENT -eq "github-hosted" -and
    $currentIdentity -match '\\runneradmin$' -and
    -not [string]::IsNullOrWhiteSpace($runnerTemp) -and
    $RepoRoot.StartsWith(
      $runnerTemp + '\',
      [StringComparison]::OrdinalIgnoreCase
    ) -and
    $QuarantineRoot.StartsWith(
      $runnerTemp + '\',
      [StringComparison]::OrdinalIgnoreCase
    )
  )
  if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) {
    throw "RepoRoot is not a git clone."
  }
  if ($QuarantineRoot.StartsWith(
      $RepoRoot + '\',
      [StringComparison]::OrdinalIgnoreCase
    ) -or $RepoRoot.StartsWith(
      $QuarantineRoot + '\',
      [StringComparison]::OrdinalIgnoreCase
    ) -or $QuarantineRoot.Equals(
      $RepoRoot,
      [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "QuarantineRoot must be outside RepoRoot."
  }

  Push-Location $RepoRoot
  try {
    $tracked = @(& git status --porcelain --untracked-files=no 2> $null)
    if ($LASTEXITCODE -ne 0) { throw "git tracked-content scan failed." }
  } finally {
    Pop-Location
  }
  if ($tracked.Count -gt 0) {
    throw "Tracked changes must be preserved through normal Git workflow first."
  }

  $relativePaths = @(Invoke-GitUntrackedNul -WorkingDirectory $RepoRoot)
  if ($ExpectedCount -ge 0 -and $relativePaths.Count -ne $ExpectedCount) {
    throw "Untracked count changed since the approved snapshot."
  }
  if ($relativePaths.Count -eq 0) {
    Write-Host "[preserve-untracked] count=0 bytes=0; no mutation required." `
      -ForegroundColor Cyan
    exit 0
  }

  $records = @()
  $totalBytes = [int64]0
  foreach ($relativePath in $relativePaths) {
    if ([IO.Path]::IsPathRooted($relativePath) -or
        $relativePath.Contains([char]0)) {
      throw "Untracked scan returned an unsafe relative path."
    }
    $sourcePath = [IO.Path]::GetFullPath(
      (Join-Path $RepoRoot ($relativePath.Replace('/', '\')))
    )
    if (-not $sourcePath.StartsWith(
        $RepoRoot + '\',
        [StringComparison]::OrdinalIgnoreCase
      )) {
      throw "Untracked scan returned content outside RepoRoot."
    }
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
      throw "Untracked scan changed during preflight."
    }
    Assert-NoReparsePoint -Path $sourcePath -StopAt $RepoRoot `
      -Purpose "Untracked content"
    $item = Get-Item -LiteralPath $sourcePath -Force
    $sha256 = Get-Sha256 -Path $sourcePath
    $totalBytes += [int64]$item.Length
    $records += [pscustomobject][ordered]@{
      relativePath = $relativePath.Replace('\', '/')
      size = [int64]$item.Length
      sha256 = $sha256
      sourcePath = $sourcePath
    }
  }

  Write-Host ("[preserve-untracked] preflight count={0} bytes={1}" -f `
    $records.Count, $totalBytes) -ForegroundColor Cyan
  if (-not $PSCmdlet.ShouldProcess(
      $RepoRoot,
      ("preserve {0} untracked files in restricted quarantine" -f `
        $records.Count)
    )) {
    Write-Host "[preserve-untracked] WhatIf/declined: validation passed; no mutation." `
      -ForegroundColor Yellow
    exit 0
  }

  $stateModule = Join-Path $PSScriptRoot "deployment-state.ps1"
  if (-not (Test-Path -LiteralPath $stateModule -PathType Leaf)) {
    throw "Missing deployment-state ACL module."
  }
  . $stateModule

  if (-not (Test-Path -LiteralPath $QuarantineRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
  }
  Assert-NoReparsePoint -Path $QuarantineRoot `
    -StopAt ([IO.Path]::GetPathRoot($QuarantineRoot)) `
    -Purpose "QuarantineRoot"
  Set-Acl -LiteralPath $QuarantineRoot `
    -AclObject (New-DeploymentStateAcl -Directory)
  Assert-DeploymentStateAcl -Path $QuarantineRoot -Directory

  $sessionId = [Guid]::NewGuid().ToString("N")
  $sessionRoot = Join-Path $QuarantineRoot $sessionId
  $objectsRoot = Join-Path $sessionRoot "objects"
  New-Item -ItemType Directory -Path $objectsRoot -Force | Out-Null
  Set-Acl -LiteralPath $sessionRoot `
    -AclObject (New-DeploymentStateAcl -Directory)
  Set-Acl -LiteralPath $objectsRoot `
    -AclObject (New-DeploymentStateAcl -Directory)
  Assert-DeploymentStateAcl -Path $sessionRoot -Directory
  Assert-DeploymentStateAcl -Path $objectsRoot -Directory

  foreach ($record in $records) {
    $objectPath = Join-Path $objectsRoot $record.sha256
    if (-not (Test-Path -LiteralPath $objectPath -PathType Leaf)) {
      $tempObject = $objectPath + ".tmp-" + [Guid]::NewGuid().ToString("N")
      Copy-Item -LiteralPath $record.sourcePath -Destination $tempObject
      if ((Get-Sha256 -Path $tempObject) -ne $record.sha256 -or
          (Get-Item -LiteralPath $tempObject).Length -ne $record.size) {
        Remove-Item -LiteralPath $tempObject -Force -ErrorAction SilentlyContinue
        throw "Quarantine copy readback failed."
      }
      Move-Item -LiteralPath $tempObject -Destination $objectPath
      Set-Acl -LiteralPath $objectPath -AclObject (New-DeploymentStateAcl)
    }
    Assert-DeploymentStateAcl -Path $objectPath
    if ((Get-Sha256 -Path $objectPath) -ne $record.sha256 -or
        (Get-Item -LiteralPath $objectPath).Length -ne $record.size) {
      throw "Quarantine object readback failed."
    }
  }

  $manifestRecords = @($records | ForEach-Object {
    [pscustomobject][ordered]@{
      relativePath = $_.relativePath
      size = $_.size
      sha256 = $_.sha256
    }
  })
  $manifest = [pscustomobject][ordered]@{
    schemaVersion = 1
    createdAtUtc = [DateTime]::UtcNow.ToString("o")
    count = $manifestRecords.Count
    totalBytes = $totalBytes
    entries = $manifestRecords
  }
  $manifestPath = Join-Path $sessionRoot "manifest.json"
  [IO.File]::WriteAllText(
    $manifestPath,
    ($manifest | ConvertTo-Json -Depth 5),
    (New-Object Text.UTF8Encoding($false))
  )
  Set-Acl -LiteralPath $manifestPath -AclObject (New-DeploymentStateAcl)
  Assert-DeploymentStateAcl -Path $manifestPath
  $manifestSha256 = Get-Sha256 -Path $manifestPath
  $readback = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  if ([int]$readback.count -ne $records.Count -or
      [int64]$readback.totalBytes -ne $totalBytes -or
      @($readback.entries).Count -ne $records.Count) {
    throw "Quarantine manifest readback failed."
  }

  $receipt = [pscustomobject][ordered]@{
    schemaVersion = 1
    count = $records.Count
    totalBytes = $totalBytes
    manifestSha256 = $manifestSha256
  }
  $receiptPath = Join-Path $sessionRoot "receipt.json"
  [IO.File]::WriteAllText(
    $receiptPath,
    ($receipt | ConvertTo-Json -Depth 3),
    (New-Object Text.UTF8Encoding($false))
  )
  Set-Acl -LiteralPath $receiptPath -AclObject (New-DeploymentStateAcl)
  Assert-DeploymentStateAcl -Path $receiptPath
  if ($testFaultsEnabled -and
      $env:PLATFORM_AI_TEST_INJECT_QUARANTINE_RECEIPT_FAILURE -eq "1") {
    [IO.File]::WriteAllText(
      $receiptPath,
      '{"schemaVersion":1,"count":-1}',
      (New-Object Text.UTF8Encoding($false))
    )
    Set-Acl -LiteralPath $receiptPath -AclObject (New-DeploymentStateAcl)
  }
  $receiptReadback = Get-Content -LiteralPath $receiptPath -Raw |
    ConvertFrom-Json
  if ([int]$receiptReadback.count -ne $records.Count -or
      [int64]$receiptReadback.totalBytes -ne $totalBytes -or
      "$($receiptReadback.manifestSha256)" -ne $manifestSha256) {
    throw "Quarantine receipt readback failed."
  }

  foreach ($record in $records) {
    if (-not (Test-Path -LiteralPath $record.sourcePath -PathType Leaf) -or
        (Get-Sha256 -Path $record.sourcePath) -ne $record.sha256 -or
        (Get-Item -LiteralPath $record.sourcePath).Length -ne $record.size) {
      throw "Source changed after quarantine verification; no source deletion."
    }
  }
  foreach ($record in $records) {
    Remove-Item -LiteralPath $record.sourcePath -Force
  }
  $remaining = @(Invoke-GitUntrackedNul -WorkingDirectory $RepoRoot)
  if ($remaining.Count -ne 0) {
    throw "Deploy mirror still contains untracked files after preservation."
  }

  Write-Host ("[preserve-untracked] preserved count={0} bytes={1} " +
    "manifestSha256={2} receipt={3}" -f `
    $records.Count, $totalBytes, $manifestSha256, $receiptPath) `
    -ForegroundColor Green
  exit 0
} catch {
  Stop-Preserve $_.Exception.Message
}
