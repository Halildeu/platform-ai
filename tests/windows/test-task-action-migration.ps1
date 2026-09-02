$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$contractPath = Join-Path $repoRoot "deploy\gpu-host\task-action-contract.ps1"
$migrationPath = Join-Path $repoRoot "deploy\gpu-host\migrate-task-actions.ps1"
. $contractPath

$taskNames = @("platform-ai-live-stt", "platform-ai-meeting-ai")
$legacyRoot = "C:\Users\denetimpc\platform-ai"
$canonicalRoot = "C:\platform-ai"
$pythonExe = "C:\Python311\python.exe"
$hfHome = "C:\model-cache"
$cudaBin = "C:\cuda\bin;C:\cudnn\bin"
# CI sets RUNNER_TEMP; a developer/GPU-host run has no such variable, so the
# suite fell over at its first Join-Path (gitops#3486 harness fix). Fall back
# to the OS temp dir — CI behaviour is byte-identical (RUNNER_TEMP wins).
$runnerTemp = $env:RUNNER_TEMP
if ([string]::IsNullOrWhiteSpace($runnerTemp)) {
    $runnerTemp = [IO.Path]::GetTempPath()
}
$tempRoot = $runnerTemp
if ([string]::IsNullOrWhiteSpace($tempRoot)) { $tempRoot = $env:TEMP }
$fixtureRoot = Join-Path $tempRoot "task-action-migration"
$backupRoot = Join-Path $fixtureRoot "backup"
$evidencePath = Join-Path $fixtureRoot "evidence.json"
$wrapperPath = Join-Path $fixtureRoot "invoke-migration.ps1"
$stdoutPath = Join-Path $fixtureRoot "stdout.log"
$stderrPath = Join-Path $fixtureRoot "stderr.log"
$mutexHolderPath = Join-Path $fixtureRoot "hold-migration-mutex.ps1"
$mutexReadyPath = Join-Path $fixtureRoot "migration-mutex-ready"
$createdCanonicalRoot = -not (Test-Path -LiteralPath $canonicalRoot)
$createdLegacyRoot = -not (Test-Path -LiteralPath $legacyRoot)
$folder = $null

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-IndependentStringSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($Value)
        ))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-IndependentInvariantHash {
    param([Parameter(Mandatory = $true)][string]$Xml)

    $document = New-Object Xml.XmlDocument
    $document.PreserveWhitespace = $true
    $document.LoadXml($Xml)
    $namespace = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespace.AddNamespace("t", $document.DocumentElement.NamespaceURI)
    foreach ($xpath in @("/t:Task/t:Actions", "/t:Task/t:RegistrationInfo")) {
        $node = $document.SelectSingleNode($xpath, $namespace)
        if ($null -ne $node) { [void]$node.ParentNode.RemoveChild($node) }
    }
    return Get-IndependentStringSha256 -Value $document.OuterXml
}

function Convert-TestIdentityToSid {
    param([Parameter(Mandatory = $true)][string]$Identity)

    try {
        return (New-Object Security.Principal.SecurityIdentifier($Identity)).Value
    } catch {
        return (New-Object Security.Principal.NTAccount($Identity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
}

function Assert-HardenedAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $acl = Get-Acl -LiteralPath $Path
    Assert-True $acl.AreAccessRulesProtected "Backup ACL inheritance is enabled."
    $allowed = @("S-1-5-18", "S-1-5-32-544")
    $seen = @{}
    foreach ($rule in @($acl.Access)) {
        $sid = Convert-TestIdentityToSid -Identity $rule.IdentityReference.Value
        Assert-True (-not $rule.IsInherited -and $allowed -contains $sid -and
            $rule.AccessControlType -eq `
                [Security.AccessControl.AccessControlType]::Allow) `
            "Backup ACL contains an unexpected rule."
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        Assert-True (($rule.FileSystemRights -band $fullControl) -eq $fullControl) `
            "Backup ACL principal lacks FullControl."
        $seen[$sid] = $true
    }
    foreach ($sid in $allowed) {
        Assert-True $seen.ContainsKey($sid) "Backup ACL misses a required SID."
    }
}

function Assert-PropertySet {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $wanted = @($Expected | Sort-Object)
    Assert-True (($actual -join "|") -eq ($wanted -join "|")) `
        "$Label schema changed unexpectedly."
}

function Assert-ContractInvalid {
    param(
        [string]$TaskName,
        [string]$Execute,
        [string]$Arguments,
        [string]$WorkingDirectory = ""
    )
    $result = Get-GpuHostTaskActionContract -TaskName $TaskName `
        -Execute $Execute -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    Assert-True (-not $result.Valid) "Unexpectedly accepted an invalid task action."
}

function Get-ActionArguments {
    param([string]$TaskName, [string]$RepoRoot)

    $parameters = @{
        TaskName = $TaskName
        RepoRoot = $RepoRoot
        PythonExe = $pythonExe
    }
    if ($TaskName -eq "platform-ai-live-stt") {
        $parameters.HfHome = $hfHome
        $parameters.CudaBin = $cudaBin
    }
    return New-GpuHostTaskActionArguments @parameters
}

function Register-TestTask {
    param(
        [Parameter(Mandatory = $true)]$Service,
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Arguments
    )

    try { $Folder.DeleteTask($TaskName, 0) } catch {}
    $definition = $Service.NewTask(0)
    $definition.RegistrationInfo.Description = "platform-ai CI migration contract"
    $definition.Principal.UserId = "SYSTEM"
    $definition.Principal.LogonType = 5
    $definition.Principal.RunLevel = 1
    $definition.Settings.Enabled = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = "PT0S"
    $trigger = $definition.Triggers.Create(1)
    $trigger.StartBoundary = "2099-01-01T00:00:00"
    $action = $definition.Actions.Create(0)
    # Legacy fixture: migration may recognize this bare executable only long
    # enough to rewrite it to the absolute trusted Windows PowerShell path.
    $action.Path = "powershell.exe"
    $action.Arguments = $Arguments
    $action.WorkingDirectory = ""
    [void]$Folder.RegisterTaskDefinition(
        $TaskName, $definition, 6, "SYSTEM", $null, 5, $null
    )
}

function Wait-TaskRunning {
    param([Parameter(Mandatory = $true)]$Folder, [string]$TaskName)

    $task = $Folder.GetTask($TaskName)
    [void]$task.Run($null)
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $task = $Folder.GetTask($TaskName)
        $instances = @($task.GetInstances(0))
    } while (($instances.Count -ne 1 -or [int]$task.State -ne 4) -and
        [DateTime]::UtcNow -lt $deadline)
    Assert-True ($instances.Count -eq 1 -and [int]$task.State -eq 4) `
        "Test task did not reach one running instance."
    return [int]$instances[0].EnginePID
}

function Get-TestTaskPort {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    if ($TaskName -eq "platform-ai-live-stt") { return 8200 }
    if ($TaskName -eq "platform-ai-meeting-ai") { return 8300 }
    throw "Unsupported test task."
}

function Wait-ListenerPid {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    $port = Get-TestTaskPort -TaskName $TaskName
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $pids = @(Get-NetTCPConnection -State Listen -LocalPort $port `
            -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } while ($pids.Count -ne 1 -and [DateTime]::UtcNow -lt $deadline)
    Assert-True ($pids.Count -eq 1) "Test listener did not start."
    return [int]$pids[0]
}

function Get-IndependentTaskInvariantHash {
    param([Parameter(Mandatory = $true)]$Folder, [string]$TaskName)

    return Get-IndependentInvariantHash `
        -Xml ([string]$Folder.GetTask($TaskName).Xml)
}

function Get-RegisteredContract {
    param([Parameter(Mandatory = $true)]$Folder, [string]$TaskName)

    $task = $Folder.GetTask($TaskName)
    $action = $task.Definition.Actions.Item(1)
    # The fixture intentionally models the legacy bare powershell.exe action.
    # Keep this exception scoped to migration readback; deployment validation
    # continues to require the absolute trusted Windows PowerShell path.
    return Get-GpuHostTaskActionContract -TaskName $TaskName `
        -Execute ([string]$action.Path) -Arguments ([string]$action.Arguments) `
        -WorkingDirectory ([string]$action.WorkingDirectory) -AllowBarePowerShell
}

function Set-RegisteredActionArguments {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [string]$TaskName,
        [string]$Arguments
    )

    $task = $Folder.GetTask($TaskName)
    $definition = $task.Definition
    $definition.Actions.Item(1).Path = Get-GpuHostWindowsPowerShellPath
    $definition.Actions.Item(1).Arguments = $Arguments
    $definition.Actions.Item(1).WorkingDirectory = ""
    $sddl = [string]$task.GetSecurityDescriptor(15)
    [void]$Folder.RegisterTaskDefinition(
        $TaskName, $definition, 4, "SYSTEM", $null, 5, $sddl
    )
}

function Invoke-Migration {
    param(
        [switch]$InjectFailure,
        [switch]$Crash,
        [switch]$WhatIf,
        [switch]$AllowMissingEvidence,
        [string]$EvidenceTarget = $evidencePath
    )

    $whatIfToken = ""
    if ($WhatIf) { $whatIfToken = " -WhatIf" }
    $command = @"
& '$($migrationPath.Replace("'", "''"))' -BackupRoot '$($backupRoot.Replace("'", "''"))' -EvidencePath '$($EvidenceTarget.Replace("'", "''"))' -Confirm:`$false$whatIfToken
exit `$LASTEXITCODE
"@
    [IO.File]::WriteAllText(
        $wrapperPath,
        $command,
        (New-Object Text.UTF8Encoding($false))
    )
    Remove-Item -LiteralPath $evidencePath, $stdoutPath, $stderrPath `
        -Force -ErrorAction SilentlyContinue
    $process = $null
    $oldInjection = $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST
    $oldCrash = $env:PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST
    try {
        if ($InjectFailure) {
            $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST = "1"
        } else {
            Remove-Item Env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST `
                -ErrorAction SilentlyContinue
        }
        if ($Crash) {
            $env:PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST = "1"
        } else {
            Remove-Item Env:PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST `
                -ErrorAction SilentlyContinue
        }
        $process = Start-Process powershell.exe -NoNewWindow -Wait -PassThru `
            -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                ('"{0}"' -f $wrapperPath)
            ) -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    } finally {
        if ($null -eq $oldInjection) {
            Remove-Item Env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST `
                -ErrorAction SilentlyContinue
        } else {
            $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST = $oldInjection
        }
        if ($null -eq $oldCrash) {
            Remove-Item Env:PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST `
                -ErrorAction SilentlyContinue
        } else {
            $env:PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST = $oldCrash
        }
    }
    $output = ""
    if (Test-Path -LiteralPath $stdoutPath) {
        $output += [IO.File]::ReadAllText($stdoutPath)
    }
    if (Test-Path -LiteralPath $stderrPath) {
        $output += [IO.File]::ReadAllText($stderrPath)
    }
    if ($Crash) {
        Assert-True (-not (Test-Path -LiteralPath $evidencePath)) `
            "Abrupt process exit unexpectedly wrote final evidence."
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Evidence = $null
            Output = $output
        }
    }
    if ($AllowMissingEvidence -and
        -not (Test-Path -LiteralPath $evidencePath -PathType Leaf)) {
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Evidence = $null
            Output = $output
        }
    }
    Assert-True (Test-Path -LiteralPath $evidencePath -PathType Leaf) `
        "Migration did not write evidence. Output: $output"
    $evidenceJson = [IO.File]::ReadAllText($evidencePath)
    foreach ($forbidden in @("C:\", "-RepoRoot", "<Task", "powershell.exe")) {
        Assert-True ($evidenceJson.IndexOf(
            $forbidden, [StringComparison]::OrdinalIgnoreCase
        ) -lt 0) `
            "Uploaded evidence contains forbidden task data."
    }
    $evidence = $evidenceJson | ConvertFrom-Json
    Assert-PropertySet -Value $evidence -Label "Evidence" -Expected @(
        "schemaVersion", "timestampUtc", "status", "failureClass",
        "mutationApplied", "rollbackAttempted", "rollbackSucceeded",
        "backupCreated", "transactionRecovered", "transactionPhase",
        "tasks", "privacy"
    )
    Assert-PropertySet -Value $evidence.privacy -Label "Privacy" -Expected @(
        "containsTaskArguments", "containsTaskXml", "containsAudio",
        "containsTranscript", "containsSecrets"
    )
    foreach ($taskEvidence in @($evidence.tasks)) {
        Assert-PropertySet -Value $taskEvidence -Label "Task evidence" -Expected @(
            "taskName", "beforeRepoClass", "afterRepoClass", "changeRequired",
            "actionChanged", "backupXmlSha256", "invariantBeforeSha256",
            "invariantAfterSha256", "securityBeforeSha256",
            "securityAfterSha256", "runningPidsBefore", "runningPidsAfter",
            "sameRunningProcess", "listenerPidBefore", "listenerPidAfter",
            "listenerParentPidsBefore", "listenerParentPidsAfter",
            "listenerIdentityBeforeSha256", "listenerIdentityAfterSha256",
            "sameListenerProcess"
        )
    }
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Evidence = $evidence
        Output = $output
    }
}

function Get-RunningPid {
    param([Parameter(Mandatory = $true)]$Folder, [string]$TaskName)

    $instances = @($Folder.GetTask($TaskName).GetInstances(0))
    Assert-True ($instances.Count -eq 1) "Running instance count changed."
    return [int]$instances[0].EnginePID
}

try {
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
    foreach ($root in @($legacyRoot, $canonicalRoot)) {
        $deployDir = Join-Path $root "deploy\gpu-host"
        New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
        foreach ($scriptName in @("start-live-stt.ps1", "start-meeting-ai.ps1")) {
            $port = 8300
            if ($scriptName -eq "start-live-stt.ps1") { $port = 8200 }
            $dummy = @'
param(
    [string]$RepoRoot,
    [string]$PythonExe,
    [string]$HfHome,
    [string]$CudaBin
)
$childCode = @"
`$listener = New-Object Net.Sockets.TcpListener(
    [Net.IPAddress]::Loopback, __PORT__
)
`$listener.Start()
try {
    while (`$true) { Start-Sleep -Seconds 1 }
} finally {
    `$listener.Stop()
}
"@
$encoded = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($childCode)
)
$child = Start-Process powershell.exe -PassThru -WindowStyle Hidden `
    -ArgumentList @("-NoProfile", "-EncodedCommand", $encoded)
$child.WaitForExit()
'@
            $dummy = $dummy.Replace("__PORT__", [string]$port)
            [IO.File]::WriteAllText(
                (Join-Path $deployDir $scriptName),
                $dummy,
                (New-Object Text.UTF8Encoding($false))
            )
        }
    }

    $liveLegacy = Get-ActionArguments -TaskName "platform-ai-live-stt" `
        -RepoRoot $legacyRoot
    $meetingLegacy = Get-ActionArguments -TaskName "platform-ai-meeting-ai" `
        -RepoRoot $legacyRoot
    foreach ($case in @(
        @{ Name = "platform-ai-live-stt"; Args = $liveLegacy },
        @{ Name = "platform-ai-meeting-ai"; Args = $meetingLegacy }
    )) {
        $contract = Get-GpuHostTaskActionContract -TaskName $case.Name `
            -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Arguments $case.Args
        Assert-True $contract.Valid "Exact legacy task action was rejected."
        Assert-True ($contract.RepoClass -eq "legacy-user-repo") `
            "Legacy action was classified incorrectly."
        Assert-True ($contract.CanonicalArguments -eq `
            (Get-ActionArguments -TaskName $case.Name -RepoRoot $canonicalRoot)) `
            "Legacy action did not target the migration root."
    }
    $customRoot = "D:\platform-ai"
    $customArguments = Get-ActionArguments -TaskName "platform-ai-live-stt" `
        -RepoRoot $customRoot
    $customContract = Get-GpuHostTaskActionContract `
        -TaskName "platform-ai-live-stt" `
        -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Arguments $customArguments
    Assert-True $customContract.Valid `
        "An installer-supported absolute repository root was rejected."
    Assert-True ($customContract.RepoClass -eq "canonical-repo") `
        "A non-legacy repository root was classified incorrectly."
    Assert-True ($customContract.RepoRoot -eq $customRoot) `
        "The parsed custom repository root was not exposed."
    Assert-True ($customContract.CanonicalArguments -eq $customArguments) `
        "Canonical arguments were not generated from the parsed repository root."

    $mismatchedRootArguments = $customArguments.Replace(
        "-RepoRoot D:\platform-ai -PythonExe",
        "-RepoRoot C:\platform-ai -PythonExe"
    )
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" `
        -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Arguments $mismatchedRootArguments
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "cmd.exe" `
        -Arguments $liveLegacy
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" `
        -Execute "C:\untrusted\powershell.exe" -Arguments $liveLegacy
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "powershell.exe" `
        -Arguments ($liveLegacy + " -Unexpected value")
    $mismatchedLegacyArguments = $liveLegacy.Replace(
        "-RepoRoot $legacyRoot -PythonExe",
        "-RepoRoot C:\other-repo -PythonExe"
    )
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "powershell.exe" `
        -Arguments $mismatchedLegacyArguments
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "powershell.exe" `
        -Arguments $liveLegacy -WorkingDirectory $legacyRoot

    # start-meeting-ai.ps1 treats the task action as the authoritative deployed
    # environment and throws when an approved runtime config declares another
    # one. A contract that cannot carry -AppEnv therefore cannot describe a host
    # deployed as "test": canonicalisation would drop the token and silently
    # fall back to the "stage" parameter default.
    $meetingTestEnv = New-GpuHostTaskActionArguments `
        -TaskName "platform-ai-meeting-ai" -RepoRoot $canonicalRoot `
        -PythonExe $pythonExe -AppEnv "test"
    Assert-True ($meetingTestEnv -match "-AppEnv test") `
        "Meeting AI canonical arguments dropped the application environment."
    $meetingEnvContract = Get-GpuHostTaskActionContract `
        -TaskName "platform-ai-meeting-ai" `
        -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Arguments $meetingTestEnv
    Assert-True $meetingEnvContract.Valid `
        "A meeting AI action carrying -AppEnv was rejected."
    Assert-True ($meetingEnvContract.AppEnv -eq "test") `
        "The parsed application environment did not survive the contract."
    Assert-True ($meetingEnvContract.CanonicalArguments -eq $meetingTestEnv) `
        "Canonicalisation rewrote the deployed application environment."

    # Omitting it stays valid: the launcher default then applies, unchanged.
    $meetingNoEnv = New-GpuHostTaskActionArguments `
        -TaskName "platform-ai-meeting-ai" -RepoRoot $canonicalRoot `
        -PythonExe $pythonExe
    Assert-True ($meetingNoEnv -notmatch "-AppEnv") `
        "An unset application environment must not be materialised."

    Assert-ContractInvalid -TaskName "platform-ai-meeting-ai" `
        -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Arguments ($meetingNoEnv + " -AppEnv production")
    Assert-ContractInvalid -TaskName "platform-ai-meeting-ai" `
        -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Arguments ($meetingNoEnv + " -AppEnv")

    $liveEnvRejected = $false
    try {
        New-GpuHostTaskActionArguments -TaskName "platform-ai-live-stt" `
            -RepoRoot $canonicalRoot -PythonExe $pythonExe -HfHome $hfHome `
            -AppEnv "test" | Out-Null
    } catch { $liveEnvRejected = $true }
    Assert-True $liveEnvRejected `
        "Live STT must not accept an application environment."

    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    Register-TestTask -Service $service -Folder $folder `
        -TaskName "platform-ai-live-stt" -Arguments $liveLegacy
    Register-TestTask -Service $service -Folder $folder `
        -TaskName "platform-ai-meeting-ai" -Arguments $meetingLegacy
    $pidsBefore = @{}
    $listenerPidsBefore = @{}
    $invariantsBefore = @{}
    foreach ($taskName in $taskNames) {
        $pidsBefore[$taskName] = Wait-TaskRunning -Folder $folder -TaskName $taskName
        $listenerPidsBefore[$taskName] = Wait-ListenerPid -TaskName $taskName
        Assert-True ($listenerPidsBefore[$taskName] -ne $pidsBefore[$taskName]) `
            "Test fixture did not separate task and listener processes."
        $invariantsBefore[$taskName] = Get-IndependentTaskInvariantHash `
            -Folder $folder -TaskName $taskName
    }

    $escapedReadyPath = $mutexReadyPath.Replace("'", "''")
    $mutexHolder = @"
`$ErrorActionPreference = "Stop"
`$mutex = New-Object Threading.Mutex(
    `$false, "Global\platform-ai-task-action-migration-v1"
)
`$taken = `$false
try {
    `$taken = `$mutex.WaitOne(0)
    if (-not `$taken) { throw "fixture-lock-unavailable" }
    [IO.File]::WriteAllText('$escapedReadyPath', "ready")
    Start-Sleep -Seconds 60
} finally {
    if (`$taken) { [void]`$mutex.ReleaseMutex() }
    `$mutex.Dispose()
}
"@
    [IO.File]::WriteAllText(
        $mutexHolderPath, $mutexHolder, (New-Object Text.UTF8Encoding($false))
    )
    Remove-Item -LiteralPath $mutexReadyPath -Force -ErrorAction SilentlyContinue
    $mutexProcess = Start-Process powershell.exe -PassThru -WindowStyle Hidden `
        -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            ('"{0}"' -f $mutexHolderPath)
        )
    try {
        $mutexDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not (Test-Path -LiteralPath $mutexReadyPath -PathType Leaf) -and
            [DateTime]::UtcNow -lt $mutexDeadline) {
            Start-Sleep -Milliseconds 100
        }
        Assert-True (Test-Path -LiteralPath $mutexReadyPath -PathType Leaf) `
            "Migration mutex fixture did not become ready."
        $blockedByMutex = Invoke-Migration -WhatIf
        Assert-True ($blockedByMutex.ExitCode -ne 0 -and
            $blockedByMutex.Evidence.status -eq "no-go" -and
            $blockedByMutex.Evidence.failureClass -eq "migration_already_running" -and
            -not $blockedByMutex.Evidence.mutationApplied) `
            ("Concurrent migration did not fail closed. exit={0} status={1} failureClass={2} mutationApplied={3}" -f `
                $blockedByMutex.ExitCode,
                $blockedByMutex.Evidence.status,
                $blockedByMutex.Evidence.failureClass,
                $blockedByMutex.Evidence.mutationApplied)
    } finally {
        # Killing the only owner destroys the named kernel object once all handles
        # close. The next invocation must create/acquire the same name successfully.
        if (-not $mutexProcess.HasExited) {
            Stop-Process -Id $mutexProcess.Id -Force
        }
        [void]$mutexProcess.WaitForExit(10000)
        Remove-Item -LiteralPath $mutexReadyPath -Force -ErrorAction SilentlyContinue
    }

    $whatIf = Invoke-Migration -WhatIf
    Assert-True ($whatIf.ExitCode -eq 0 -and $whatIf.Evidence.status -eq "ready") `
        (("WhatIf did not produce ready evidence: exit={0} status={1} " +
            "failure={2} output={3}") -f $whatIf.ExitCode,
            $whatIf.Evidence.status, $whatIf.Evidence.failureClass, $whatIf.Output)
    foreach ($taskName in $taskNames) {
        Assert-True ((Get-RegisteredContract -Folder $folder `
            -TaskName $taskName).RepoClass -eq "legacy-user-repo") `
            "WhatIf mutated a task action."
        Assert-True ((Get-RunningPid -Folder $folder -TaskName $taskName) -eq `
            $pidsBefore[$taskName]) "WhatIf changed a running process."
        Assert-True ((Wait-ListenerPid -TaskName $taskName) -eq `
            $listenerPidsBefore[$taskName]) "WhatIf changed a listener process."
        Assert-True ((Get-IndependentTaskInvariantHash -Folder $folder `
            -TaskName $taskName) -eq $invariantsBefore[$taskName]) `
            "WhatIf changed an independently measured invariant."
    }

    # Keep the fixture outside BackupRoot: creating an arbitrary child there before
    # the migration hardens the root would correctly trip BACKUP_ACL_INVALID instead
    # of reaching the intended evidence-write failure path.
    $unwritableEvidenceTarget = Join-Path $fixtureRoot "evidence-target-directory"
    New-Item -ItemType Directory -Path $unwritableEvidenceTarget -Force | Out-Null
    $evidenceWriteFailed = Invoke-Migration -WhatIf -AllowMissingEvidence `
        -EvidenceTarget $unwritableEvidenceTarget
    Assert-True ($evidenceWriteFailed.ExitCode -ne 0) `
        "Evidence write failure fixture unexpectedly succeeded."
    $afterEvidenceFailure = Invoke-Migration -WhatIf
    Assert-True ($afterEvidenceFailure.ExitCode -eq 0 -and
        $afterEvidenceFailure.Evidence.status -eq "ready") `
        (("Evidence write failure follow-up was not ready. exit={0} status={1} " +
            "failureClass={2} output={3}") -f $afterEvidenceFailure.ExitCode,
            $afterEvidenceFailure.Evidence.status,
            $afterEvidenceFailure.Evidence.failureClass,
            $afterEvidenceFailure.Output)

    $applied = Invoke-Migration
    Assert-True ($applied.ExitCode -eq 0 -and $applied.Evidence.status -eq "go") `
        "Task action migration failed. Output: $($applied.Output)"
    Assert-True $applied.Evidence.mutationApplied "Migration did not report mutation."
    Assert-True ($applied.Evidence.tasks.Count -eq 2) `
        "Migration evidence must contain exactly two tasks."
    foreach ($taskName in $taskNames) {
        Assert-True ((Get-RegisteredContract -Folder $folder `
            -TaskName $taskName).RepoClass -eq "canonical-repo") `
            "Task action did not migrate to the canonical repository."
        Assert-True ((Get-RunningPid -Folder $folder -TaskName $taskName) -eq `
            $pidsBefore[$taskName]) "Migration restarted a running process."
        Assert-True ((Wait-ListenerPid -TaskName $taskName) -eq `
            $listenerPidsBefore[$taskName]) "Migration replaced a listener process."
        Assert-True ((Get-IndependentTaskInvariantHash -Folder $folder `
            -TaskName $taskName) -eq $invariantsBefore[$taskName]) `
            "Migration changed an independently measured invariant."
        $taskEvidence = @($applied.Evidence.tasks | Where-Object {
            $_.taskName -eq $taskName
        })[0]
        Assert-True ($taskEvidence.sameRunningProcess -and
            $taskEvidence.sameListenerProcess) `
            "Evidence did not prove both process identities stayed stable."
    }

    $appliedBatch = @(Get-ChildItem -LiteralPath $backupRoot -Directory |
        Sort-Object LastWriteTimeUtc -Descending)[0]
    Assert-HardenedAcl -Path $backupRoot
    Assert-HardenedAcl -Path $appliedBatch.FullName
    foreach ($taskName in $taskNames) {
        $taskEvidence = @($applied.Evidence.tasks | Where-Object {
            $_.taskName -eq $taskName
        })[0]
        $xmlPath = Join-Path $appliedBatch.FullName ("{0}.xml" -f $taskName)
        $sddlPath = Join-Path $appliedBatch.FullName ("{0}.sddl" -f $taskName)
        foreach ($path in @($xmlPath, $sddlPath)) { Assert-HardenedAcl -Path $path }
        Assert-True ((Get-IndependentStringSha256 -Value `
            ([IO.File]::ReadAllText($xmlPath, [Text.Encoding]::UTF8))
        ) -eq $taskEvidence.backupXmlSha256) `
            "Backup XML hash does not match independent verification."
    }
    Assert-HardenedAcl -Path (Join-Path $appliedBatch.FullName "transaction.json")
    Assert-True (-not (Test-Path -LiteralPath `
        (Join-Path $backupRoot "active-transaction.json"))) `
        "Committed transaction left an active pointer."

    $idempotent = Invoke-Migration
    Assert-True ($idempotent.ExitCode -eq 0 -and $idempotent.Evidence.status -eq "go") `
        "Idempotent migration run failed."
    Assert-True (-not $idempotent.Evidence.mutationApplied) `
        "Idempotent migration unexpectedly mutated task actions."

    Set-RegisteredActionArguments -Folder $folder `
        -TaskName "platform-ai-live-stt" -Arguments $liveLegacy
    Set-RegisteredActionArguments -Folder $folder `
        -TaskName "platform-ai-meeting-ai" -Arguments $meetingLegacy
    $rolledBack = Invoke-Migration -InjectFailure
    Assert-True ($rolledBack.ExitCode -ne 0 -and `
        $rolledBack.Evidence.status -eq "no-go") `
        "Injected failure did not fail closed."
    Assert-True ($rolledBack.Evidence.rollbackAttempted -and `
        $rolledBack.Evidence.rollbackSucceeded) `
        "Injected failure did not restore both task definitions."
    foreach ($taskName in $taskNames) {
        Assert-True ((Get-RegisteredContract -Folder $folder `
            -TaskName $taskName).RepoClass -eq "legacy-user-repo") `
            "Rollback did not restore the legacy task definition."
        Assert-True ((Get-RunningPid -Folder $folder -TaskName $taskName) -eq `
            $pidsBefore[$taskName]) "Rollback changed a running process."
        Assert-True ((Wait-ListenerPid -TaskName $taskName) -eq `
            $listenerPidsBefore[$taskName]) "Rollback changed a listener process."
        Assert-True ((Get-IndependentTaskInvariantHash -Folder $folder `
            -TaskName $taskName) -eq $invariantsBefore[$taskName]) `
            "Rollback changed an independently measured invariant."
    }

    $crashed = Invoke-Migration -Crash
    Assert-True ($crashed.ExitCode -eq 9) `
        "Crash injection did not terminate the migration process abruptly."
    Assert-True ((Get-RegisteredContract -Folder $folder `
        -TaskName "platform-ai-live-stt").RepoClass -eq "canonical-repo") `
        "Crash fixture did not stop after the first mutation."
    Assert-True ((Get-RegisteredContract -Folder $folder `
        -TaskName "platform-ai-meeting-ai").RepoClass -eq "legacy-user-repo") `
        "Crash fixture unexpectedly applied the second mutation."
    Assert-True (Test-Path -LiteralPath `
        (Join-Path $backupRoot "active-transaction.json") -PathType Leaf) `
        "Abrupt process exit did not leave a recovery pointer."

    $recoveryWhatIf = Invoke-Migration -WhatIf
    Assert-True ($recoveryWhatIf.ExitCode -eq 1 -and
        $recoveryWhatIf.Evidence.status -eq "no-go" -and
        $recoveryWhatIf.Evidence.failureClass -eq `
            "interrupted_transaction_recovery_required" -and
        -not $recoveryWhatIf.Evidence.mutationApplied -and
        -not $recoveryWhatIf.Evidence.rollbackAttempted) `
        "WhatIf did not fail closed on an interrupted transaction."
    Assert-True (Test-Path -LiteralPath `
        (Join-Path $backupRoot "active-transaction.json") -PathType Leaf) `
        "WhatIf removed the interrupted transaction recovery pointer."
    Assert-True ((Get-RegisteredContract -Folder $folder `
        -TaskName "platform-ai-live-stt").RepoClass -eq "canonical-repo") `
        "WhatIf recovered the first partially migrated task."
    Assert-True ((Get-RegisteredContract -Folder $folder `
        -TaskName "platform-ai-meeting-ai").RepoClass -eq "legacy-user-repo") `
        "WhatIf changed the second task during recovery inspection."

    $recovered = Invoke-Migration
    Assert-True ($recovered.ExitCode -eq 2 -and
        $recovered.Evidence.status -eq "recovered" -and
        $recovered.Evidence.transactionRecovered -and
        $recovered.Evidence.rollbackSucceeded) `
        "The next invocation did not recover the interrupted transaction."
    foreach ($taskName in $taskNames) {
        Assert-True ((Get-RegisteredContract -Folder $folder `
            -TaskName $taskName).RepoClass -eq "legacy-user-repo") `
            "Crash recovery did not restore the legacy task definition."
        Assert-True ((Get-RunningPid -Folder $folder -TaskName $taskName) -eq `
            $pidsBefore[$taskName]) "Crash recovery changed a task process."
        Assert-True ((Wait-ListenerPid -TaskName $taskName) -eq `
            $listenerPidsBefore[$taskName]) "Crash recovery changed a listener process."
        Assert-True ((Get-IndependentTaskInvariantHash -Folder $folder `
            -TaskName $taskName) -eq $invariantsBefore[$taskName]) `
            "Crash recovery changed an independently measured invariant."
    }
    Assert-True (-not (Test-Path -LiteralPath `
        (Join-Path $backupRoot "active-transaction.json"))) `
        "Crash recovery left an active transaction pointer."

    Write-Host "GPU-host Scheduled Task action migration contract: PASS"
} finally {
    try {
        if ($null -ne $folder) {
            foreach ($taskName in $taskNames) {
                try {
                    $port = Get-TestTaskPort -TaskName $taskName
                    Get-NetTCPConnection -State Listen -LocalPort $port `
                        -ErrorAction SilentlyContinue |
                        Select-Object -ExpandProperty OwningProcess -Unique |
                        ForEach-Object {
                            Stop-Process -Id ([int]$_) -Force -ErrorAction SilentlyContinue
                        }
                } catch {}
                try { $folder.GetTask($taskName).Stop(0) } catch {}
                try { $folder.DeleteTask($taskName, 0) } catch {}
            }
        }
    } catch {}
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($createdLegacyRoot -and (Test-Path -LiteralPath $legacyRoot)) {
        Remove-Item -LiteralPath $legacyRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($createdCanonicalRoot -and (Test-Path -LiteralPath $canonicalRoot)) {
        Remove-Item -LiteralPath $canonicalRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
