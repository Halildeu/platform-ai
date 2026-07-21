$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $repoRoot "deploy\gpu-host\task-action-contract.ps1")
. (Join-Path $repoRoot "deploy\gpu-host\restart-acceptance.ps1")
. (Join-Path $repoRoot "deploy\gpu-host\live-stt-runtime-env.ps1")

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Message)
    $threw = $false
    try { & $Action } catch { $threw = $true }
    Assert-True $threw $Message
}

$failedQuery = {
    param($port)
    return New-GpuHostOwnerResult -Succeeded $false -Reason "injected"
}
$clock = [Diagnostics.Stopwatch]::StartNew()
$release = Wait-GpuHostPortReleased -Port 8200 -Clock $clock -DeadlineSec 1 `
    -OwnerQuery $failedQuery
Assert-True (-not $release.Succeeded) "Owner query failure must fail release acceptance."

$staleQuery = {
    param($port)
    return New-GpuHostOwnerResult -Succeeded $true -Owners @(41)
}
$clock = [Diagnostics.Stopwatch]::StartNew()
$stale = Wait-GpuHostNewPortOwner -Port 8200 -PreviousOwners @(41) `
    -Clock $clock -DeadlineSec 0.1 -OwnerQuery $staleQuery -StableSamples 1
Assert-True (-not $stale.Succeeded) "A stale owner must not satisfy restart acceptance."

$script:ownerSequence = @(51, 52, 51, 52)
$script:ownerIndex = 0
$churnQuery = {
    param($port)
    $owner = $script:ownerSequence[$script:ownerIndex % $script:ownerSequence.Count]
    $script:ownerIndex++
    return New-GpuHostOwnerResult -Succeeded $true -Owners @($owner)
}
$clock = [Diagnostics.Stopwatch]::StartNew()
$churn = Wait-GpuHostNewPortOwner -Port 8200 -PreviousOwners @(41) `
    -Clock $clock -DeadlineSec 0.8 -OwnerQuery $churnQuery -StableSamples 3
Assert-True (-not $churn.Succeeded) "PID churn must not satisfy stable-owner acceptance."

$processes = @{
    100 = [pscustomobject]@{
        ProcessId = 100
        ParentProcessId = 200
        ExecutablePath = "C:\Python311\python.exe"
        CommandLine = '"C:\Python311\python.exe" -m uvicorn app.main:app --port 8200'
        CreationDate = "20260721220000.000000+000"
    }
    200 = [pscustomobject]@{
        ProcessId = 200
        ParentProcessId = 4
        ExecutablePath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        CommandLine = "powershell.exe"
        CreationDate = "20260721215959.000000+000"
    }
}
$processQuery = { param($processId) return $processes[[int]$processId] }
$mismatch = Get-GpuHostListenerIdentityProof -ProcessId 100 `
    -ExpectedPythonExe "C:\Other\python.exe" -ExpectedPort 8200 `
    -ExpectedTaskPids @(200) -ProcessQuery $processQuery
Assert-True (-not $mismatch.Succeeded) "Interpreter mismatch must fail closed."

$proof = Get-GpuHostListenerIdentityProof -ProcessId 100 `
    -ExpectedPythonExe "C:\Python311\python.exe" -ExpectedPort 8200 `
    -ExpectedTaskPids @(200) -ProcessQuery $processQuery
Assert-True $proof.Succeeded "Exact interpreter, command, and task ancestry must pass."

$stableOwner = {
    param($port)
    return New-GpuHostOwnerResult -Succeeded $true -Owners @(100)
}
$stableProof = {
    param($processId, $pythonExe, $port, $taskPids)
    return [pscustomobject]@{
        Succeeded = $true
        ProcessId = $processId
        IdentitySha256 = "0123456789abcdef"
    }
}
$clock = [Diagnostics.Stopwatch]::StartNew()
Assert-True (Test-GpuHostListenerStable -Port 8200 -ExpectedOwnerId 100 `
    -ExpectedPythonExe "C:\Python311\python.exe" -ExpectedTaskPids @(200) `
    -Clock $clock -DeadlineSec 3 -OwnerQuery $stableOwner `
    -ProcessProofQuery $stableProof -StableSamples 2) `
    "Stable exact listener proof must pass."

$tempRoot = Join-Path $env:RUNNER_TEMP "live-stt-runtime-env"
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$configPath = Join-Path $tempRoot "live-stt.env"
$oldCi = $env:CI
try {
    $env:CI = "true"
    [IO.File]::WriteAllText($configPath, '$RepoRoot=C:\attacker', (New-Object Text.UTF8Encoding($false)))
    Assert-Throws {
        Import-LiveSttRuntimeEnvironment -ConfigPath $configPath -SkipAclValidation
    } "Executable PowerShell-like config must be rejected."

    [IO.File]::WriteAllText(
        $configPath,
        "STT_CHUNK_CONSUMER_ENABLED=false`nSTT_CHUNK_CONSUMER_ENABLED=true",
        (New-Object Text.UTF8Encoding($false))
    )
    Assert-Throws {
        Import-LiveSttRuntimeEnvironment -ConfigPath $configPath -SkipAclValidation
    } "Duplicate config keys must be rejected."

    $redis = "redis://:synthetic-ci-secret@127.0.0.1:6379/0"
    $ciphertext = [Security.Cryptography.ProtectedData]::Protect(
        [Text.Encoding]::UTF8.GetBytes($redis),
        $script:LiveSttDpapiEntropy,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $blob = [Convert]::ToBase64String($ciphertext)
    [IO.File]::WriteAllText(
        $configPath,
        "STT_CHUNK_CONSUMER_ENABLED=true`nSTT_REDIS_URL_DPAPI=$blob",
        (New-Object Text.UTF8Encoding($false))
    )
    Clear-LiveSttManagedProcessEnvironment
    Assert-True (Import-LiveSttRuntimeEnvironment -ConfigPath $configPath `
        -SkipAclValidation) "Valid strict config must import."
    Assert-True ($env:STT_CHUNK_CONSUMER_ENABLED -eq "true") `
        "Boolean config was not imported."
    Assert-True ($env:STT_REDIS_URL -eq $redis) "DPAPI Redis URL was not imported."
} finally {
    Clear-LiveSttManagedProcessEnvironment
    $env:CI = $oldCi
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "live-stt restart/runtime acceptance contract: PASS"
