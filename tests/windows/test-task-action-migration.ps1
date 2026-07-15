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
$tempRoot = $env:RUNNER_TEMP
if ([string]::IsNullOrWhiteSpace($tempRoot)) { $tempRoot = $env:TEMP }
$fixtureRoot = Join-Path $tempRoot "task-action-migration"
$backupRoot = Join-Path $fixtureRoot "backup"
$evidencePath = Join-Path $fixtureRoot "evidence.json"
$wrapperPath = Join-Path $fixtureRoot "invoke-migration.ps1"
$stdoutPath = Join-Path $fixtureRoot "stdout.log"
$stderrPath = Join-Path $fixtureRoot "stderr.log"
$createdCanonicalRoot = -not (Test-Path -LiteralPath $canonicalRoot)
$createdLegacyRoot = -not (Test-Path -LiteralPath $legacyRoot)
$folder = $null

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
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

function Get-RegisteredContract {
    param([Parameter(Mandatory = $true)]$Folder, [string]$TaskName)

    $task = $Folder.GetTask($TaskName)
    $action = $task.Definition.Actions.Item(1)
    return Get-GpuHostTaskActionContract -TaskName $TaskName `
        -Execute ([string]$action.Path) -Arguments ([string]$action.Arguments) `
        -WorkingDirectory ([string]$action.WorkingDirectory)
}

function Set-RegisteredActionArguments {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [string]$TaskName,
        [string]$Arguments
    )

    $task = $Folder.GetTask($TaskName)
    $definition = $task.Definition
    $definition.Actions.Item(1).Path = "powershell.exe"
    $definition.Actions.Item(1).Arguments = $Arguments
    $definition.Actions.Item(1).WorkingDirectory = ""
    $sddl = [string]$task.GetSecurityDescriptor(7)
    [void]$Folder.RegisterTaskDefinition(
        $TaskName, $definition, 4, "SYSTEM", $null, 5, $sddl
    )
}

function Invoke-Migration {
    param([switch]$InjectFailure, [switch]$WhatIf)

    $whatIfToken = ""
    if ($WhatIf) { $whatIfToken = " -WhatIf" }
    $command = @"
& '$($migrationPath.Replace("'", "''"))' -BackupRoot '$($backupRoot.Replace("'", "''"))' -EvidencePath '$($evidencePath.Replace("'", "''"))' -Confirm:`$false$whatIfToken
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
    $oldCi = $env:CI
    $oldInjection = $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST
    try {
        $env:CI = "true"
        if ($InjectFailure) {
            $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST = "1"
        } else {
            Remove-Item Env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST `
                -ErrorAction SilentlyContinue
        }
        $process = Start-Process powershell.exe -NoNewWindow -Wait -PassThru `
            -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                ('"{0}"' -f $wrapperPath)
            ) -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    } finally {
        if ($null -eq $oldCi) { Remove-Item Env:CI -ErrorAction SilentlyContinue }
        else { $env:CI = $oldCi }
        if ($null -eq $oldInjection) {
            Remove-Item Env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST `
                -ErrorAction SilentlyContinue
        } else {
            $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST = $oldInjection
        }
    }
    $output = ""
    if (Test-Path -LiteralPath $stdoutPath) {
        $output += [IO.File]::ReadAllText($stdoutPath)
    }
    if (Test-Path -LiteralPath $stderrPath) {
        $output += [IO.File]::ReadAllText($stderrPath)
    }
    Assert-True (Test-Path -LiteralPath $evidencePath -PathType Leaf) `
        "Migration did not write evidence. Output: $output"
    $evidenceJson = [IO.File]::ReadAllText($evidencePath)
    Assert-True (-not $evidenceJson.Contains("C:\")) `
        "Uploaded evidence contains a Windows path."
    Assert-True (-not $evidenceJson.Contains("-RepoRoot")) `
        "Uploaded evidence contains task arguments."
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Evidence = ($evidenceJson | ConvertFrom-Json)
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
            $dummy = @'
param(
    [string]$RepoRoot,
    [string]$PythonExe,
    [string]$HfHome,
    [string]$CudaBin
)
Start-Sleep -Seconds 300
'@
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
    }
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "cmd.exe" `
        -Arguments $liveLegacy
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "powershell.exe" `
        -Arguments ($liveLegacy + " -Unexpected value")
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "powershell.exe" `
        -Arguments ($liveLegacy.Replace($legacyRoot, "C:\other-repo"))
    Assert-ContractInvalid -TaskName "platform-ai-live-stt" -Execute "powershell.exe" `
        -Arguments $liveLegacy -WorkingDirectory $legacyRoot

    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    Register-TestTask -Service $service -Folder $folder `
        -TaskName "platform-ai-live-stt" -Arguments $liveLegacy
    Register-TestTask -Service $service -Folder $folder `
        -TaskName "platform-ai-meeting-ai" -Arguments $meetingLegacy
    $pidsBefore = @{}
    foreach ($taskName in $taskNames) {
        $pidsBefore[$taskName] = Wait-TaskRunning -Folder $folder -TaskName $taskName
    }

    $whatIf = Invoke-Migration -WhatIf
    Assert-True ($whatIf.ExitCode -eq 0 -and $whatIf.Evidence.status -eq "ready") `
        "WhatIf did not produce ready evidence."
    foreach ($taskName in $taskNames) {
        Assert-True ((Get-RegisteredContract -Folder $folder `
            -TaskName $taskName).RepoClass -eq "legacy-user-repo") `
            "WhatIf mutated a task action."
        Assert-True ((Get-RunningPid -Folder $folder -TaskName $taskName) -eq `
            $pidsBefore[$taskName]) "WhatIf changed a running process."
    }

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
    }

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
    }

    Write-Host "GPU-host Scheduled Task action migration contract: PASS"
} finally {
    try {
        if ($null -ne $folder) {
            foreach ($taskName in $taskNames) {
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
