$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$preserveScript = Join-Path $repoRoot `
    "deploy\gpu-host\preserve-untracked.ps1"
$stateModule = Join-Path $repoRoot "deploy\gpu-host\deployment-state.ps1"
$tempRoot = $env:RUNNER_TEMP
if ([string]::IsNullOrWhiteSpace($tempRoot)) { $tempRoot = $env:TEMP }
$fixtureRoot = Join-Path $tempRoot "platform-ai-untracked-quarantine"
$source = Join-Path $fixtureRoot "source"
$quarantine = Join-Path $fixtureRoot "quarantine"

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-Preserve {
    param([string[]]$ExtraArgs)

    $id = [Guid]::NewGuid().ToString("N")
    $stdout = Join-Path $tempRoot ("preserve-{0}.stdout" -f $id)
    $stderr = Join-Path $tempRoot ("preserve-{0}.stderr" -f $id)
    $tokens = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $preserveScript),
        "-RepoRoot", ('"{0}"' -f $source),
        "-QuarantineRoot", ('"{0}"' -f $quarantine)
    ) + $ExtraArgs
    try {
        $process = Start-Process powershell.exe -NoNewWindow -Wait -PassThru `
            -ArgumentList $tokens `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr
        $output = @()
        if (Test-Path -LiteralPath $stdout) {
            $output += @(Get-Content -LiteralPath $stdout)
        }
        if (Test-Path -LiteralPath $stderr) {
            $output += @(Get-Content -LiteralPath $stderr)
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Output = $output
        }
    } finally {
        Remove-Item -LiteralPath $stdout, $stderr -Force `
            -ErrorAction SilentlyContinue
    }
}

function Write-PrivateFixture {
    $nested = Join-Path $source "private-fixtures"
    New-Item -ItemType Directory -Path $nested -Force | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path $source "private-a.wav"),
        [Text.Encoding]::UTF8.GetBytes("same-private-content")
    )
    [IO.File]::WriteAllBytes(
        (Join-Path $nested "private-b.rttm"),
        [Text.Encoding]::UTF8.GetBytes("same-private-content")
    )
    [IO.File]::WriteAllBytes(
        (Join-Path $nested "private-c.exe"),
        [Text.Encoding]::UTF8.GetBytes("different-private-content")
    )
}

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Assert-True $isAdmin "quarantine ACL test requires an elevated Windows runner"

if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
& git init $source *> $null
Assert-True ($LASTEXITCODE -eq 0) "fixture git init failed"
& git -C $source config user.email "ci@example.invalid"
& git -C $source config user.name "CI Fixture"
[IO.File]::WriteAllText(
    (Join-Path $source "tracked.txt"),
    "tracked",
    (New-Object Text.UTF8Encoding($false))
)
& git -C $source add tracked.txt
& git -C $source commit -m "fixture tracked source" *> $null
Assert-True ($LASTEXITCODE -eq 0) "fixture commit failed"

Write-PrivateFixture
$whatIf = Invoke-Preserve @("-ExpectedCount", "3", "-WhatIf")
Assert-True ($whatIf.ExitCode -eq 0) "quarantine WhatIf failed"
Assert-True (-not (Test-Path -LiteralPath $quarantine)) `
    "quarantine WhatIf created the destination"
Assert-True (Test-Path -LiteralPath (Join-Path $source "private-a.wav")) `
    "quarantine WhatIf removed source content"

$mismatch = Invoke-Preserve @("-ExpectedCount", "4")
Assert-True ($mismatch.ExitCode -eq 2) `
    "changed untracked count must fail before mutation"
Assert-True (-not (Test-Path -LiteralPath $quarantine)) `
    "count mismatch created quarantine state"

$env:CI = "true"
$env:GITHUB_ACTIONS = "true"
$env:RUNNER_ENVIRONMENT = "github-hosted"
$env:PLATFORM_AI_TEST_INJECT_QUARANTINE_RECEIPT_FAILURE = "1"
$readbackFailure = Invoke-Preserve @("-ExpectedCount", "3")
Assert-True ($readbackFailure.ExitCode -eq 2) `
    "injected receipt readback failure must fail"
Assert-True (Test-Path -LiteralPath (Join-Path $source "private-a.wav")) `
    "receipt readback failure removed source content"
Remove-Item Env:PLATFORM_AI_TEST_INJECT_QUARANTINE_RECEIPT_FAILURE

$preserved = Invoke-Preserve @("-ExpectedCount", "3")
Assert-True ($preserved.ExitCode -eq 0) `
    "verified quarantine preservation failed"
$joinedOutput = $preserved.Output -join " | "
foreach ($privateName in @("private-a.wav", "private-b.rttm", "private-c.exe")) {
    Assert-True (-not $joinedOutput.Contains($privateName)) `
        "status output disclosed a source filename"
}
Assert-True (-not (Test-Path -LiteralPath (Join-Path $source "private-a.wav"))) `
    "verified source was not removed"
$remaining = @(& git -C $source ls-files --others --exclude-standard)
Assert-True ($LASTEXITCODE -eq 0 -and $remaining.Count -eq 0) `
    "deploy mirror still contains untracked files"

. $stateModule
$validReceipts = @()
foreach ($candidate in @(
    Get-ChildItem -LiteralPath $quarantine -Filter "receipt.json" -Recurse
)) {
    try {
        $value = Get-Content -LiteralPath $candidate.FullName -Raw |
            ConvertFrom-Json
        if ([int]$value.count -eq 3) {
            $validReceipts += [pscustomobject]@{
                Path = $candidate.FullName
                Value = $value
            }
        }
    } catch { }
}
Assert-True ($validReceipts.Count -eq 1) `
    "expected one verified quarantine receipt"
$receiptPath = $validReceipts[0].Path
$receipt = $validReceipts[0].Value
$sessionRoot = Split-Path -Parent $receiptPath
$manifestPath = Join-Path $sessionRoot "manifest.json"
$objectsRoot = Join-Path $sessionRoot "objects"
Assert-DeploymentStateAcl -Path $sessionRoot -Directory
Assert-DeploymentStateAcl -Path $objectsRoot -Directory
Assert-DeploymentStateAcl -Path $receiptPath
Assert-DeploymentStateAcl -Path $manifestPath
$manifestSha = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
Assert-True ($manifestSha.ToLowerInvariant() -eq
    "$($receipt.manifestSha256)") "receipt manifest digest mismatch"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
Assert-True ([int]$manifest.count -eq 3 -and
    @($manifest.entries).Count -eq 3) "manifest count mismatch"
Assert-True (@(
    Get-ChildItem -LiteralPath $objectsRoot -File
).Count -eq 2) "content-addressed quarantine did not deduplicate objects"
foreach ($entry in @($manifest.entries)) {
    $objectPath = Join-Path $objectsRoot "$($entry.sha256)"
    Assert-DeploymentStateAcl -Path $objectPath
    $objectSha = (Get-FileHash -LiteralPath $objectPath -Algorithm SHA256).Hash
    Assert-True ($objectSha.ToLowerInvariant() -eq "$($entry.sha256)") `
        "quarantine object digest mismatch"
    Assert-True ((Get-Item -LiteralPath $objectPath).Length -eq
        [int64]$entry.size) "quarantine object size mismatch"
}

Write-Host "untracked quarantine contract: PASS"
