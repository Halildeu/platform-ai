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

$startScriptPath = Join-Path $repoRoot "deploy\gpu-host\start-live-stt.ps1"
$startScriptSource = [IO.File]::ReadAllText($startScriptPath)
$runtimeImportMarker = '$null = Import-LiveSttRuntimeEnvironment'
$runtimeImportIndex = $startScriptSource.IndexOf(
    $runtimeImportMarker,
    [StringComparison]::Ordinal
)
Assert-True ($runtimeImportIndex -gt 0) `
    "The launcher must import the validated runtime environment."
$beforeRuntimeImport = $startScriptSource.Substring(0, $runtimeImportIndex)
$afterRuntimeImport = $startScriptSource.Substring(
    $runtimeImportIndex + $runtimeImportMarker.Length
)
foreach ($requiredBeforeImport in @(
        'STT_SPEECH_GATE_PROFILE',
        'STT_SILENCE_RMS',
        'STT_MIN_SPEECH_RMS',
        'STT_STREAM_LIVE_VAD_FILTER',
        'STT_STREAM_FINAL_VAD_FILTER'
    )) {
    Assert-True ($beforeRuntimeImport.Contains($requiredBeforeImport)) `
        "$requiredBeforeImport must be sourced before host-local overrides."
}
foreach ($requiredAfterImport in @(
        'STT_SPEECH_GATE_PROFILE',
        'STT_STREAM_LIVE_VAD_FILTER',
        'STT_STREAM_FINAL_VAD_FILTER',
        'STT_STREAM_VAD_THRESHOLD',
        'STT_STREAM_VAD_MIN_SPEECH_DURATION_MS',
        'STT_STREAM_VAD_MIN_SILENCE_DURATION_MS',
        'STT_STREAM_VAD_SPEECH_PAD_MS',
        'STT_LIVE_INFER_INTERVAL_MS',
        'STT_LIVE_WINDOW_SEC',
        'STT_FINAL_WINDOW_SEC',
        'STT_MIN_INFER_SEC'
    )) {
    Assert-True ($afterRuntimeImport.Contains($requiredAfterImport)) `
        "$requiredAfterImport must be re-asserted after host-local overrides."
}
Assert-True ($afterRuntimeImport -notmatch `
    '\$env:STT_(SILENCE_RMS|MIN_SPEECH_RMS)\s*=') `
    "The launcher must not overwrite the validated host-local RMS pair."

function New-TestLiveSttAcl {
    param([switch]$Directory)

    $acl = if ($Directory) {
        New-Object Security.AccessControl.DirectorySecurity
    } else {
        New-Object Security.AccessControl.FileSecurity
    }
    $system = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $administrators = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) {
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    foreach ($sid in @($system, $administrators)) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    return $acl
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
$directTaskAction = Get-GpuHostTaskActionContract `
    -TaskName "platform-ai-live-stt" -Execute (Get-GpuHostWindowsPowerShellPath) `
    -Arguments $taskArguments
Assert-True $directTaskAction.Valid `
    "Canonical direct Scheduled Task action contract must pass."
$validTaskXml = Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $taskXml -SkipPythonPathValidation
Assert-True $validTaskXml.Valid `
    ("Canonical single SYSTEM task action must pass: {0}" -f $validTaskXml.Reason)
Assert-True ($validTaskXml.RepoRoot -eq "C:\platform-ai") `
    "Task XML contract did not expose the parsed repository root."
Assert-True (Test-GpuHostSameLocalPath -Left "C:\platform-ai" `
    -Right "c:\PLATFORM-AI\") `
    "Canonical task root comparison must be case-insensitive and slash-stable."
Assert-True (-not (Test-GpuHostSameLocalPath -Left "C:\platform-ai" `
    -Right "C:\Users\denetimpc\platform-ai")) `
    "Legacy task root must not match the canonical deployment checkout."
$workingDirectoryXml = $taskXml.Replace(
    "</Exec>",
    "<WorkingDirectory>C:\platform-ai</WorkingDirectory></Exec>"
)
$workingDirectoryAction = Get-GpuHostTaskXmlContract `
    -TaskName "platform-ai-live-stt" -TaskXml $workingDirectoryXml `
    -SkipPythonPathValidation
Assert-True (-not $workingDirectoryAction.Valid) `
    "A non-empty Scheduled Task working directory must fail closed."
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

# The XML Windows actually emits for a LocalSystem task: Task Scheduler omits
# elements holding their default value, and it never writes LogonType for
# S-1-5-18 because LocalSystem has no interactive logon. The fixture above
# spells it out, which is why CI accepted a reader that could not parse the
# real thing -- on the GPU host every deploy was rejected with the raw error
# "The property 'LogonType' cannot be found on this object" instead of a
# verdict, disabling both tasks.
$omittedLogonTypeXml = $taskXml.Replace(
    "<LogonType>ServiceAccount</LogonType>", ""
)
$omittedLogonType = Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $omittedLogonTypeXml -SkipPythonPathValidation
Assert-True $omittedLogonType.Valid `
    ("A LocalSystem principal without an explicit LogonType is the canonical " +
     "Windows form and must pass: {0}" -f $omittedLogonType.Reason)

# The implied default is granted to LocalSystem only; every other way of
# reaching the same state still fails closed.
$omittedLogonNonSystemXml = $omittedLogonTypeXml.Replace(
    "S-1-5-18", "S-1-5-32-544"
)
Assert-True (-not (Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $omittedLogonNonSystemXml -SkipPythonPathValidation).Valid) `
    "A missing LogonType must not imply a service logon for a non-SYSTEM user."
$explicitWrongLogonXml = $taskXml.Replace(
    "<LogonType>ServiceAccount</LogonType>",
    "<LogonType>InteractiveToken</LogonType>"
)
Assert-True (-not (Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $explicitWrongLogonXml -SkipPythonPathValidation).Valid) `
    "An explicit non-service LogonType must still fail closed."
$omittedRunLevelXml = $taskXml.Replace(
    "<RunLevel>HighestAvailable</RunLevel>", ""
)
Assert-True (-not (Get-GpuHostTaskXmlContract -TaskName "platform-ai-live-stt" `
    -TaskXml $omittedRunLevelXml -SkipPythonPathValidation).Valid) `
    "A missing RunLevel means LeastPrivilege and must fail closed."

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
    Set-Acl -LiteralPath $tempRoot -AclObject (New-TestLiveSttAcl -Directory)
    [IO.File]::WriteAllText($configPath, '$RepoRoot=C:\attacker', (New-Object Text.UTF8Encoding($false)))
    Set-Acl -LiteralPath $configPath -AclObject (New-TestLiveSttAcl)
    Assert-LiveSttRuntimeConfigAcl -Path $tempRoot -Directory
    Assert-LiveSttRuntimeConfigAcl -Path $configPath
    $wrongOwnerAcl = Get-Acl -LiteralPath $configPath
    $wrongOwnerAcl.SetOwner([Security.Principal.WindowsIdentity]::GetCurrent().User)
    Set-Acl -LiteralPath $configPath -AclObject $wrongOwnerAcl
    Assert-Throws {
        Assert-LiveSttRuntimeConfigAcl -Path $configPath
    } "A non-SYSTEM/non-Administrators secret owner must fail closed."
    Set-Acl -LiteralPath $configPath -AclObject (New-TestLiveSttAcl)
    $allowedRuntimePath = Join-Path (Get-LiveSttRuntimeRoot) "ci-contract.env"
    Assert-True ((Assert-LiveSttRuntimeConfigPath -Path $allowedRuntimePath) -eq `
        [IO.Path]::GetFullPath($allowedRuntimePath)) `
        "The fixed local ProgramData runtime path should pass."
    Assert-Throws {
        Assert-LiveSttRuntimeConfigPath -Path $configPath
    } "A config outside the hardened ProgramData root must fail closed."
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

    $schema = Get-LiveSttRuntimeConfigSchema
    foreach ($rmsKey in @("STT_SILENCE_RMS", "STT_MIN_SPEECH_RMS")) {
        foreach ($invalidRms in @("NaN", "Infinity", "1e-3", "0,001", "+0.001", `
                " 0.001", "0.000500", "0.00001", "0.051")) {
            Assert-Throws {
                ConvertFrom-LiveSttRuntimeValue -Key $rmsKey `
                    -Value $invalidRms -Spec $schema[$rmsKey]
            } "Non-canonical or out-of-range RMS must be rejected."
        }
        foreach ($boundaryRms in @("0.0001", "0.05")) {
            $converted = ConvertFrom-LiveSttRuntimeValue -Key $rmsKey `
                -Value $boundaryRms -Spec $schema[$rmsKey]
            Assert-True ($converted -eq $boundaryRms) `
                "Inclusive RMS boundary was not accepted exactly."
        }
    }

    $env:STT_SILENCE_RMS = "0.0005"
    $env:STT_MIN_SPEECH_RMS = "0.0005"
    $env:STT_SPEECH_GATE_RMS_SOURCE = "source-baseline"
    foreach ($partialRms in @(
            "STT_SILENCE_RMS=0.0001",
            "STT_MIN_SPEECH_RMS=0.001"
        )) {
        [IO.File]::WriteAllText(
            $configPath,
            $partialRms,
            (New-Object Text.UTF8Encoding($false))
        )
        Assert-Throws {
            Import-LiveSttRuntimeEnvironment -ConfigPath $configPath -SkipAclValidation
        } "A one-sided RMS override must fail atomically."
        Assert-True ($env:STT_SILENCE_RMS -eq "0.0005" -and `
            $env:STT_MIN_SPEECH_RMS -eq "0.0005") `
            "A rejected RMS pair partially mutated the process environment."
        Assert-True ($env:STT_SPEECH_GATE_RMS_SOURCE -eq "source-baseline") `
            "A rejected RMS pair changed the public RMS source marker."
    }

    foreach ($boundedPair in @(
            "STT_SILENCE_RMS=0.0001`nSTT_MIN_SPEECH_RMS=0.0001",
            "STT_SILENCE_RMS=0.05`nSTT_MIN_SPEECH_RMS=0.05"
        )) {
        [IO.File]::WriteAllText(
            $configPath,
            $boundedPair,
            (New-Object Text.UTF8Encoding($false))
        )
        Assert-True (Import-LiveSttRuntimeEnvironment -ConfigPath $configPath `
            -SkipAclValidation) "An inclusive RMS boundary pair must import."
        $env:STT_SILENCE_RMS = "0.0005"
        $env:STT_MIN_SPEECH_RMS = "0.0005"
        $env:STT_SPEECH_GATE_RMS_SOURCE = "source-baseline"
    }

    [IO.File]::WriteAllText(
        $configPath,
        "STT_SILENCE_RMS=0.02`nSTT_MIN_SPEECH_RMS=0.01",
        (New-Object Text.UTF8Encoding($false))
    )
    Assert-Throws {
        Import-LiveSttRuntimeEnvironment -ConfigPath $configPath -SkipAclValidation
    } "A reversed RMS pair must fail atomically."

    [IO.File]::WriteAllText(
        $configPath,
        "STT_SILENCE_RMS=0.01`nSTT_MIN_SPEECH_RMS=0.015",
        (New-Object Text.UTF8Encoding($false))
    )
    Assert-True (Import-LiveSttRuntimeEnvironment -ConfigPath $configPath `
        -SkipAclValidation) "A valid bounded RMS pair must import."
    Assert-True ($env:STT_SILENCE_RMS -eq "0.01" -and `
        $env:STT_MIN_SPEECH_RMS -eq "0.015") `
        "The validated RMS override did not replace the source baseline."
    Assert-True ($env:STT_SPEECH_GATE_RMS_SOURCE -eq "host-override") `
        "A validated RMS override did not expose its public source marker."
    Clear-LiveSttManagedProcessEnvironment
    Remove-Item Env:STT_SPEECH_GATE_RMS_SOURCE -ErrorAction SilentlyContinue

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

$configureFixtureRoot = Join-Path $env:RUNNER_TEMP "live-stt-configure"
$configureDeployRoot = Join-Path $configureFixtureRoot "deploy\gpu-host"
$configureProgramData = Join-Path $configureFixtureRoot "ProgramData"
$configureScript = Join-Path $configureDeployRoot "configure-live-stt.ps1"
$configureRuntimeModule = Join-Path $configureDeployRoot "live-stt-runtime-env.ps1"
$oldProgramData = $env:ProgramData
$legacyFixture = Join-Path $configureDeployRoot "env.local.ps1"
$script:LiveSttLegacyExecuted = $false
try {
    Remove-Item -LiteralPath $configureFixtureRoot -Recurse -Force `
        -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $configureDeployRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $configureProgramData | Out-Null
    Copy-Item -LiteralPath (Join-Path $repoRoot `
        "deploy\gpu-host\configure-live-stt.ps1") -Destination $configureScript
    Copy-Item -LiteralPath (Join-Path $repoRoot `
        "deploy\gpu-host\live-stt-runtime-env.ps1") -Destination $configureRuntimeModule
    Copy-Item -LiteralPath (Join-Path $repoRoot `
        "deploy\gpu-host\live-stt-runtime-contract.ps1") -Destination `
        (Join-Path $configureDeployRoot "live-stt-runtime-contract.ps1")
    $configureSource = [IO.File]::ReadAllText($configureScript)
    Assert-True (-not ($configureSource -match '(?m)^\s*\[string\]\$ConfigPath')) `
        "The live STT provisioner must not expose a configurable runtime path."
    Assert-True (-not $configureSource.Contains(". `$legacyConfigPath")) `
        "The live STT provisioner must never dot-source the legacy config."
    Assert-True ($configureSource.Contains("DataProtectionScope]::LocalMachine")) `
        "The live STT provisioner must use DPAPI LocalMachine."

    $env:ProgramData = $configureProgramData
    $firstRedis = "redis://:synthetic-provision-secret-one@127.0.0.1:6379/0"
    $firstSecure = ConvertTo-SecureString $firstRedis -AsPlainText -Force
    $firstOutput = @(& $configureScript -RepoRoot $configureFixtureRoot `
        -RedisUrl $firstSecure `
        -ChunkConsumerEnabled true -RequestTimeout 180 `
        -ChunkStreamPrefix "audio:chunks:p" -ChunkPartitionCount 32 `
        -ChunkConsumerGroup "live-stt-v1" -ChunkConsumerName "gpu-host-1" `
        -ChunkBlockMs 2000 -ChunkBatchSize 16 -ChunkDedupCacheSize 8192 `
        -ChunkClaimIdleMs 60000 -ChunkClaimEveryLoops 30 `
        -ChunkTrimMaxlen 10000 6>&1)
    Assert-True (-not (($firstOutput | Out-String).Contains($firstRedis))) `
        "Provisioning output exposed the Redis credential."

    $configuredPath = Join-Path $configureProgramData `
        "Acik\platform-ai\live-stt.env"
    Assert-True (Test-Path -LiteralPath $configuredPath -PathType Leaf) `
        "The provisioner did not write the fixed ProgramData config."
    Assert-LiveSttRuntimeConfigAcl -Path (Join-Path $configureProgramData "Acik") `
        -Directory
    Assert-LiveSttRuntimeConfigAcl -Path (Join-Path $configureProgramData `
        "Acik\platform-ai") -Directory
    Assert-LiveSttRuntimeConfigAcl -Path $configuredPath
    $firstContent = [IO.File]::ReadAllText($configuredPath)
    Assert-True (-not $firstContent.Contains($firstRedis)) `
        "The runtime config contains a plaintext Redis credential."
    Assert-True ($firstContent.Contains("STT_REDIS_URL_DPAPI=")) `
        "The runtime config misses its DPAPI Redis blob."
    Assert-True (-not $firstContent.Contains("STT_SILENCE_RMS=") -and `
        -not $firstContent.Contains("STT_MIN_SPEECH_RMS=")) `
        "Source RMS defaults must not be persisted as host-local overrides."
    Clear-LiveSttManagedProcessEnvironment
    Assert-True (Import-LiveSttRuntimeEnvironment -ConfigPath $configuredPath) `
        "The provisioned runtime config did not pass strict readback."
    Assert-True ($env:STT_REDIS_URL -eq $firstRedis) `
        "The provisioned Redis credential failed its DPAPI round trip."
    Assert-True ($env:STT_CHUNK_PARTITION_COUNT -eq "32") `
        "The provisioned public config did not round trip."
    Clear-LiveSttManagedProcessEnvironment

    $secondOutput = @(& $configureScript -RepoRoot $configureFixtureRoot 6>&1)
    $secondContent = [IO.File]::ReadAllText($configuredPath)
    Assert-True ($secondContent -ceq $firstContent) `
        "An idempotent run changed existing public config or the DPAPI blob."
    Assert-True (-not (($secondOutput | Out-String).Contains($firstRedis))) `
        "Idempotent provisioning output exposed the Redis credential."

    Assert-Throws {
        & $configureScript -RepoRoot $configureFixtureRoot `
            -SilenceRms "0.001" 6>&1
    } "Provisioning must reject a one-sided RMS override."
    Assert-True ([IO.File]::ReadAllText($configuredPath) -ceq $firstContent) `
        "Rejected one-sided RMS provisioning changed the persisted config."

    $rmsOutput = @(& $configureScript -RepoRoot $configureFixtureRoot `
        -SilenceRms "0.001" -MinSpeechRms "0.0015" 6>&1)
    $rmsContent = [IO.File]::ReadAllText($configuredPath)
    Assert-True ($rmsContent.Contains("STT_SILENCE_RMS=0.001") -and `
        $rmsContent.Contains("STT_MIN_SPEECH_RMS=0.0015")) `
        "Explicit RMS overrides were not persisted."
    Assert-True (-not (($rmsOutput | Out-String).Contains($firstRedis))) `
        "RMS provisioning output exposed the Redis credential."

    $rotatedRedis = "rediss://:synthetic-provision-secret-two@redis.internal:6380/0"
    $rotatedSecure = ConvertTo-SecureString $rotatedRedis -AsPlainText -Force
    $rotationOutput = @(& $configureScript -RepoRoot $configureFixtureRoot `
        -RedisUrl $rotatedSecure `
        -RequestTimeout 90 6>&1)
    $rotatedContent = [IO.File]::ReadAllText($configuredPath)
    Assert-True (-not $rotatedContent.Contains($rotatedRedis)) `
        "Rotated runtime config contains a plaintext Redis credential."
    Assert-True (-not (($rotationOutput | Out-String).Contains($rotatedRedis))) `
        "Rotation output exposed the Redis credential."
    Assert-True ($rotatedContent.Contains("STT_REQUEST_TIMEOUT=90")) `
        "Explicit public config rotation was not persisted."
    Assert-True (-not (Test-Path -LiteralPath "$configuredPath.bak")) `
        "A successful atomic rotation retained its temporary backup."
    Assert-True (@(Get-ChildItem -LiteralPath (Split-Path -Parent $configuredPath) `
        -Filter ".live-stt-*.tmp" -Force).Count -eq 0) `
        "A successful atomic rotation retained a staging file."
    Clear-LiveSttManagedProcessEnvironment
    Assert-True (Import-LiveSttRuntimeEnvironment -ConfigPath $configuredPath) `
        "The rotated runtime config did not pass strict readback."
    Assert-True ($env:STT_REDIS_URL -eq $rotatedRedis) `
        "The rotated Redis credential failed its DPAPI round trip."
    Clear-LiveSttManagedProcessEnvironment

    [IO.File]::WriteAllText(
        $legacyFixture,
        '$script:LiveSttLegacyExecuted = $true; throw "legacy executed"',
        (New-Object Text.UTF8Encoding($false))
    )
    $migrationRedis = "redis://:synthetic-migration-secret@127.0.0.1:6379/1"
    $migrationSecure = ConvertTo-SecureString $migrationRedis -AsPlainText -Force
    $migrationRecords = New-Object Collections.Generic.List[string]
    $migrationError = ""
    try {
        & $configureScript -RepoRoot $configureFixtureRoot `
            -RedisUrl $migrationSecure -RequestTimeout 91 6>&1 |
            ForEach-Object { [void]$migrationRecords.Add("$_") }
    } catch {
        $migrationError = $_.Exception.Message
        [void]$migrationRecords.Add($migrationError)
    }
    Assert-True (-not [string]::IsNullOrWhiteSpace($migrationError)) `
        "A remaining legacy PowerShell config must leave migration fail-closed."
    Assert-True (Test-Path -LiteralPath $legacyFixture -PathType Leaf) `
        "The provisioner must not silently delete the legacy config."
    Assert-True (-not $script:LiveSttLegacyExecuted) `
        "The provisioner executed or dot-sourced the legacy PowerShell config."
    Assert-True (-not (($migrationRecords -join "`n").Contains($migrationRedis))) `
        "Fail-closed migration output exposed the newly supplied credential."
    Clear-LiveSttManagedProcessEnvironment
    Assert-True (Import-LiveSttRuntimeEnvironment -ConfigPath $configuredPath) `
        "The replacement DPAPI config was not verified before migration blocked."
    Assert-True ($env:STT_REDIS_URL -eq $migrationRedis) `
        "The fail-closed migration did not persist the operator-supplied secret."
    Assert-True ($env:STT_REQUEST_TIMEOUT -eq "91") `
        "The fail-closed migration did not persist the public config update."
    Clear-LiveSttManagedProcessEnvironment

    $postMigrationOutput = @(& $configureScript -RepoRoot $configureFixtureRoot `
        -RemoveLegacyAfterVerifiedMigration -Confirm:$false 6>&1)
    Assert-True (-not (Test-Path -LiteralPath $legacyFixture)) `
        "Explicit verified migration did not remove the legacy plaintext config."
    Assert-True (-not $script:LiveSttLegacyExecuted) `
        "Explicit verified migration executed the legacy PowerShell config."
    Assert-True (-not (($postMigrationOutput | Out-String).Contains($migrationRedis))) `
        "Post-migration verification output exposed the Redis credential."
} finally {
    Clear-LiveSttManagedProcessEnvironment
    $env:ProgramData = $oldProgramData
    $firstSecure = $null
    $rotatedSecure = $null
    $migrationSecure = $null
    $firstRedis = $null
    $rotatedRedis = $null
    $migrationRedis = $null
    Remove-Item -LiteralPath $configureFixtureRoot -Recurse -Force `
        -ErrorAction SilentlyContinue
}

Write-Host "live-stt restart/runtime acceptance contract: PASS"
