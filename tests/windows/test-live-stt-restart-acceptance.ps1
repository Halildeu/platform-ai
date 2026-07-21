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

$netstatFailure = ConvertFrom-GpuHostNetstatLines -Port 8200 -ExitCode 1 -Lines @()
Assert-True (-not $netstatFailure.Succeeded) `
    "netstat non-zero exit must fail closed."
$malformedNetstat = ConvertFrom-GpuHostNetstatLines -Port 8200 -ExitCode 0 `
    -Lines @(" TCP 0.0.0.0:8200 0.0.0.0:0 LISTENING not-a-pid")
Assert-True (-not $malformedNetstat.Succeeded) `
    "Malformed matching netstat output must fail closed."

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

$oldGuid = "11111111-1111-1111-1111-111111111111"
$newGuid = "22222222-2222-2222-2222-222222222222"
$oldTaskQuery = {
    param($taskName)
    return New-GpuHostTaskInstanceResult -Succeeded $true -Instances @(
        [pscustomobject]@{ InstanceGuid = $oldGuid; EnginePid = 200 }
    )
}
$clock = [Diagnostics.Stopwatch]::StartNew()
$noOpRun = Wait-GpuHostNewTaskInstance -TaskName "platform-ai-live-stt" `
    -PreviousInstanceGuids @($oldGuid) -Clock $clock -DeadlineSec 0.2 `
    -TaskInstanceQuery $oldTaskQuery -StableSamples 1
Assert-True (-not $noOpRun.Succeeded) `
    "/Run no-op exposing the old task instance must fail closed."

$newTaskQuery = {
    param($taskName)
    return New-GpuHostTaskInstanceResult -Succeeded $true -Instances @(
        [pscustomobject]@{ InstanceGuid = $newGuid; EnginePid = 300 }
    )
}
$clock = [Diagnostics.Stopwatch]::StartNew()
$newTask = Wait-GpuHostNewTaskInstance -TaskName "platform-ai-live-stt" `
    -PreviousInstanceGuids @($oldGuid) -Clock $clock -DeadlineSec 1 `
    -TaskInstanceQuery $newTaskQuery -StableSamples 2
Assert-True ($newTask.Succeeded -and $newTask.Instances.Count -eq 1) `
    "A stable new task InstanceGuid and EnginePID must pass."
$clock = [Diagnostics.Stopwatch]::StartNew()
Assert-True (Test-GpuHostTaskInstanceStable -TaskName "platform-ai-live-stt" `
    -ExpectedInstanceGuid $newGuid -ExpectedEnginePid 300 -Clock $clock `
    -DeadlineSec 1 -TaskInstanceQuery $newTaskQuery -StableSamples 2) `
    "The accepted task instance identity must remain stable."

$processes = @{
    100 = [pscustomobject]@{
        ProcessId = 100
        ParentProcessId = 200
        ExecutablePath = "C:\Python311\python.exe"
        CommandLine = '"C:\Python311\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8200'
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

$processes[100].CommandLine = `
    '"C:\Python311\python.exe" -m uvicorn other.main:app --host 0.0.0.0 --port 8200'
$wrongApp = Get-GpuHostListenerIdentityProof -ProcessId 100 `
    -ExpectedPythonExe "C:\Python311\python.exe" -ExpectedPort 8200 `
    -ExpectedTaskPids @(200) -ProcessQuery $processQuery
Assert-True (-not $wrongApp.Succeeded) `
    "A different ASGI application must not satisfy exact command binding."
$processes[100].CommandLine = `
    '"C:\Python311\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8200'

$taskArguments = New-GpuHostTaskActionArguments `
    -TaskName "platform-ai-live-stt" -RepoRoot "C:\platform-ai" `
    -PythonExe "C:\Python311\python.exe" -HfHome "C:\hf-cache"
$escapedArguments = [Security.SecurityElement]::Escape($taskArguments)
$escapedPowerShell = [Security.SecurityElement]::Escape(
    (Get-GpuHostWindowsPowerShellPath)
)
$taskXml = @"
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><LogonType>ServiceAccount</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions Context="Author"><Exec><Command>$escapedPowerShell</Command><Arguments>$escapedArguments</Arguments></Exec></Actions>
</Task>
"@
$validTaskXml = Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $taskXml -SkipPythonPathValidation
Assert-True $validTaskXml.Valid "Canonical single SYSTEM task action must pass."
$multiActionXml = $taskXml.Replace(
    "</Actions>",
    "<Exec><Command>$escapedPowerShell</Command><Arguments>$escapedArguments</Arguments></Exec></Actions>"
)
$multiAction = Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $multiActionXml -SkipPythonPathValidation
Assert-True (-not $multiAction.Valid) "Multiple Scheduled Task actions must fail closed."
$wrongPrincipalXml = $taskXml.Replace("S-1-5-18", "S-1-5-32-544")
$wrongPrincipal = Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $wrongPrincipalXml -SkipPythonPathValidation
Assert-True (-not $wrongPrincipal.Valid) "Non-SYSTEM principal must fail closed."

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
