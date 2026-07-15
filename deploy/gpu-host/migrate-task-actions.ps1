# Migrate the two GPU-host Scheduled Task actions to C:\platform-ai without
# stopping or restarting their currently running service processes.

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$RepoRoot = "C:\platform-ai",
    [string]$BackupRoot = "C:\ProgramData\Acik\platform-ai\task-action-migration",
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$script:MigrationMarker = "PLATFORM_AI_TASK_ACTION_MIGRATION_EVIDENCE_B64="
$script:CanonicalRepoRoot = "C:\platform-ai"
$script:DefaultBackupRoot = "C:\ProgramData\Acik\platform-ai\task-action-migration"
$script:TaskUpdate = 4
$script:SecurityInformation = 7
$script:SystemSid = "S-1-5-18"
$script:AdministratorsSid = "S-1-5-32-544"
$script:Tasks = @("platform-ai-live-stt", "platform-ai-meeting-ai")

function Get-StringSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
        )).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function New-MigrationAcl {
    param([switch]$Directory)

    if ($Directory) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
    } else {
        $acl = New-Object Security.AccessControl.FileSecurity
    }
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) {
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $rights = [Security.AccessControl.FileSystemRights]::FullControl
    $system = New-Object Security.Principal.SecurityIdentifier($script:SystemSid)
    $administrators = New-Object Security.Principal.SecurityIdentifier(
        $script:AdministratorsSid
    )
    foreach ($principal in @($system, $administrators)) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $principal, $rights, $inheritance, $propagation, $allow
        )))
    }
    $acl.SetOwner($administrators)
    return $acl
}

function Initialize-BackupDirectory {
    param([Parameter(Mandatory = $true)][string]$Root)

    $fullRoot = [IO.Path]::GetFullPath($Root)
    if ($fullRoot.StartsWith("\\", [StringComparison]::Ordinal)) {
        throw "BACKUP_PATH_INVALID"
    }
    if (-not (Test-Path -LiteralPath $fullRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $fullRoot -Force | Out-Null
    }
    $rootItem = Get-Item -LiteralPath $fullRoot -Force
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "BACKUP_PATH_INVALID"
    }
    Set-Acl -LiteralPath $fullRoot -AclObject (New-MigrationAcl -Directory)

    $batch = Join-Path $fullRoot (
        "{0}-{1}" -f [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"),
        [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $batch -Force | Out-Null
    Set-Acl -LiteralPath $batch -AclObject (New-MigrationAcl -Directory)
    return $batch
}

function Write-BackupXml {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Xml
    )

    $path = Join-Path $Directory ("{0}.xml" -f $TaskName)
    [IO.File]::WriteAllText(
        $path,
        $Xml,
        (New-Object Text.UTF8Encoding($false))
    )
    Set-Acl -LiteralPath $path -AclObject (New-MigrationAcl)
    return $path
}

function Get-TaskInvariantHash {
    param([Parameter(Mandatory = $true)][string]$Xml)

    $document = New-Object Xml.XmlDocument
    $document.PreserveWhitespace = $true
    $document.LoadXml($Xml)
    $namespace = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespace.AddNamespace("t", $document.DocumentElement.NamespaceURI)
    foreach ($xpath in @("/t:Task/t:Actions", "/t:Task/t:RegistrationInfo")) {
        $node = $document.SelectSingleNode($xpath, $namespace)
        if ($null -ne $node) {
            [void]$node.ParentNode.RemoveChild($node)
        }
    }
    return Get-StringSha256 -Value $document.OuterXml
}

function Get-RunningInstancePids {
    param([Parameter(Mandatory = $true)]$Task)

    $pids = @()
    foreach ($instance in @($Task.GetInstances(0))) {
        $pids += [int]$instance.EnginePID
    }
    return @($pids | Sort-Object)
}

function Test-IntArrayEqual {
    param([int[]]$Left, [int[]]$Right)

    if ($Left.Count -ne $Right.Count) { return $false }
    for ($index = 0; $index -lt $Left.Count; $index++) {
        if ($Left[$index] -ne $Right[$index]) { return $false }
    }
    return $true
}

function Get-TaskSnapshot {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)][string]$TaskName
    )

    try {
        $task = $Folder.GetTask($TaskName)
    } catch {
        throw "TASK_MISSING"
    }
    $definition = $task.Definition
    if ([int]$definition.Actions.Count -ne 1) {
        throw "TASK_ACTION_COUNT_INVALID"
    }
    $principal = $definition.Principal
    $principalUser = [string]$principal.UserId
    if ($principalUser -notin @("SYSTEM", $script:SystemSid) -or
        [int]$principal.LogonType -ne 5 -or [int]$principal.RunLevel -ne 1) {
        throw "TASK_PRINCIPAL_INVALID"
    }
    $action = $definition.Actions.Item(1)
    $contract = Get-GpuHostTaskActionContract -TaskName $TaskName `
        -Execute ([string]$action.Path) -Arguments ([string]$action.Arguments) `
        -WorkingDirectory ([string]$action.WorkingDirectory)
    if (-not $contract.Valid) {
        throw "TASK_ACTION_UNRECOGNIZED"
    }
    $pids = @(Get-RunningInstancePids -Task $task)
    if ($pids.Count -ne 1 -or [int]$task.State -ne 4) {
        throw "TASK_PROCESS_NOT_STABLE"
    }
    $xml = [string]$task.Xml
    $sddl = [string]$task.GetSecurityDescriptor($script:SecurityInformation)
    return [pscustomobject]@{
        Name = $TaskName
        Task = $task
        Definition = $definition
        Action = $action
        Contract = $contract
        Xml = $xml
        XmlHash = Get-StringSha256 -Value $xml
        InvariantHash = Get-TaskInvariantHash -Xml $xml
        Sddl = $sddl
        SddlHash = Get-StringSha256 -Value $sddl
        PrincipalUser = $principalUser
        LogonType = [int]$principal.LogonType
        Pids = $pids
        State = [int]$task.State
    }
}

function Register-TaskDefinitionPreservingSecurity {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)]$Snapshot
    )

    [void]$Folder.RegisterTaskDefinition(
        $Snapshot.Name,
        $Snapshot.Definition,
        $script:TaskUpdate,
        $Snapshot.PrincipalUser,
        $null,
        $Snapshot.LogonType,
        $Snapshot.Sddl
    )
}

function Restore-TaskSnapshot {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)]$Snapshot
    )

    [void]$Folder.RegisterTask(
        $Snapshot.Name,
        $Snapshot.Xml,
        $script:TaskUpdate,
        $Snapshot.PrincipalUser,
        $null,
        $Snapshot.LogonType,
        $Snapshot.Sddl
    )
}

function New-TaskEvidence {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [AllowNull()]$After,
        [bool]$Changed
    )

    $afterRepoClass = $null
    $afterInvariantHash = $null
    $afterSddlHash = $null
    $afterPids = @()
    $sameProcess = $false
    if ($null -ne $After) {
        $afterRepoClass = $After.Contract.RepoClass
        $afterInvariantHash = $After.InvariantHash
        $afterSddlHash = $After.SddlHash
        $afterPids = @($After.Pids)
        $sameProcess = Test-IntArrayEqual -Left $Before.Pids -Right $After.Pids
    }
    $actionChanged = $null -ne $After -and
        $Before.Contract.RepoClass -ne $After.Contract.RepoClass
    return [ordered]@{
        taskName = $Before.Name
        beforeRepoClass = $Before.Contract.RepoClass
        afterRepoClass = $afterRepoClass
        changeRequired = $Changed
        actionChanged = $actionChanged
        backupXmlSha256 = $Before.XmlHash
        invariantBeforeSha256 = $Before.InvariantHash
        invariantAfterSha256 = $afterInvariantHash
        securityBeforeSha256 = $Before.SddlHash
        securityAfterSha256 = $afterSddlHash
        runningPidsBefore = @($Before.Pids)
        runningPidsAfter = $afterPids
        sameRunningProcess = $sameProcess
    }
}

function Write-MigrationEvidence {
    param([Parameter(Mandatory = $true)]$Evidence)

    $json = $Evidence | ConvertTo-Json -Depth 8 -Compress
    if (-not [string]::IsNullOrWhiteSpace($EvidencePath)) {
        $parent = Split-Path -Parent ([IO.Path]::GetFullPath($EvidencePath))
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        [IO.File]::WriteAllText(
            [IO.Path]::GetFullPath($EvidencePath),
            $json + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false))
        )
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    [Console]::Out.WriteLine("{0}{1}" -f $script:MigrationMarker, $encoded)
}

function Get-FailureClass {
    param([Parameter(Mandatory = $true)][string]$Message)

    foreach ($known in @(
        "IDENTITY_NOT_APPROVED", "REPO_ROOT_INVALID", "BACKUP_ROOT_INVALID",
        "BACKUP_PATH_INVALID", "TASK_MISSING", "TASK_ACTION_COUNT_INVALID",
        "TASK_PRINCIPAL_INVALID", "TASK_ACTION_UNRECOGNIZED",
        "TASK_PROCESS_NOT_STABLE", "TASK_READBACK_INVALID",
        "TASK_INVARIANT_CHANGED", "TASK_SECURITY_CHANGED",
        "TASK_PROCESS_CHANGED", "TEST_INJECTED_FAILURE", "ROLLBACK_FAILED"
    )) {
        if ($Message -like "${known}*") { return $known.ToLowerInvariant() }
    }
    return "migration-unexpected"
}

$before = @{}
$after = @{}
$changed = @{}
$rollbackAttempted = $false
$rollbackSucceeded = $false
$mutationApplied = $false
$backupCreated = $false
$failureClass = $null
$status = "no-go"

try {
    if ($RepoRoot -ine $script:CanonicalRepoRoot) {
        throw "REPO_ROOT_INVALID"
    }
    $isCiEscape = $env:CI -eq "true" -and
        $BackupRoot -ine $script:DefaultBackupRoot
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdministrator = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdministrator) { throw "IDENTITY_NOT_APPROVED" }
    if (-not $isCiEscape -and $identity.Name -notmatch '\\denetimpc$') {
        throw "IDENTITY_NOT_APPROVED"
    }
    if (-not $isCiEscape -and $BackupRoot -ine $script:DefaultBackupRoot) {
        throw "BACKUP_ROOT_INVALID"
    }

    $contractPath = Join-Path $PSScriptRoot "task-action-contract.ps1"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "TASK_ACTION_UNRECOGNIZED"
    }
    . $contractPath

    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    foreach ($taskName in $script:Tasks) {
        $before[$taskName] = Get-TaskSnapshot -Folder $folder -TaskName $taskName
        $changed[$taskName] = $before[$taskName].Contract.RepoClass -ne "canonical-repo"
    }

    $needsMutation = @($script:Tasks | Where-Object { $changed[$_] }).Count -gt 0
    if (-not $needsMutation) {
        foreach ($taskName in $script:Tasks) {
            $after[$taskName] = Get-TaskSnapshot -Folder $folder -TaskName $taskName
        }
        $status = "go"
    } elseif (-not $PSCmdlet.ShouldProcess(
        "platform-ai GPU-host Scheduled Tasks",
        "replace only the two task actions and preserve running processes"
    )) {
        foreach ($taskName in $script:Tasks) {
            $after[$taskName] = $before[$taskName]
        }
        $status = "ready"
    } else {
        $backupDirectory = Initialize-BackupDirectory -Root $BackupRoot
        $backupCreated = $true
        foreach ($taskName in $script:Tasks) {
            [void](Write-BackupXml -Directory $backupDirectory -TaskName $taskName `
                -Xml $before[$taskName].Xml)
        }

        $mutationIndex = 0
        foreach ($taskName in $script:Tasks) {
            if (-not $changed[$taskName]) { continue }
            $snapshot = $before[$taskName]
            $snapshot.Action.Path = "powershell.exe"
            $snapshot.Action.Arguments = $snapshot.Contract.CanonicalArguments
            $snapshot.Action.WorkingDirectory = ""
            Register-TaskDefinitionPreservingSecurity -Folder $folder -Snapshot $snapshot
            $mutationApplied = $true
            $mutationIndex += 1
            if ($isCiEscape -and $mutationIndex -eq 1 -and
                $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST -eq "1") {
                throw "TEST_INJECTED_FAILURE"
            }
        }

        foreach ($taskName in $script:Tasks) {
            $after[$taskName] = Get-TaskSnapshot -Folder $folder -TaskName $taskName
            if ($after[$taskName].Contract.RepoClass -ne "canonical-repo") {
                throw "TASK_READBACK_INVALID"
            }
            if ($after[$taskName].InvariantHash -ne $before[$taskName].InvariantHash) {
                throw "TASK_INVARIANT_CHANGED"
            }
            if ($after[$taskName].SddlHash -ne $before[$taskName].SddlHash) {
                throw "TASK_SECURITY_CHANGED"
            }
            if (-not (Test-IntArrayEqual -Left $before[$taskName].Pids `
                -Right $after[$taskName].Pids)) {
                throw "TASK_PROCESS_CHANGED"
            }
        }
        $status = "go"
    }
} catch {
    $failureClass = Get-FailureClass -Message $_.Exception.Message
    if ($mutationApplied -and $before.Count -eq $script:Tasks.Count) {
        $rollbackAttempted = $true
        try {
            foreach ($taskName in $script:Tasks) {
                Restore-TaskSnapshot -Folder $folder -Snapshot $before[$taskName]
            }
            foreach ($taskName in $script:Tasks) {
                $restored = Get-TaskSnapshot -Folder $folder -TaskName $taskName
                if ($restored.XmlHash -ne $before[$taskName].XmlHash -or
                    $restored.InvariantHash -ne $before[$taskName].InvariantHash -or
                    $restored.SddlHash -ne $before[$taskName].SddlHash -or
                    -not (Test-IntArrayEqual -Left $restored.Pids `
                        -Right $before[$taskName].Pids)) {
                    throw "ROLLBACK_FAILED"
                }
                $after[$taskName] = $restored
            }
            $rollbackSucceeded = $true
        } catch {
            $failureClass = "rollback-failed"
            $rollbackSucceeded = $false
        }
    }
} finally {
    $taskEvidence = @()
    foreach ($taskName in $script:Tasks) {
        if ($before.ContainsKey($taskName)) {
            $afterSnapshot = $null
            if ($after.ContainsKey($taskName)) { $afterSnapshot = $after[$taskName] }
            $taskEvidence += New-TaskEvidence -Before $before[$taskName] `
                -After $afterSnapshot -Changed ([bool]$changed[$taskName])
        }
    }
    $evidence = [ordered]@{
        schemaVersion = 1
        timestampUtc = [DateTime]::UtcNow.ToString("o")
        status = $status
        failureClass = $failureClass
        mutationApplied = $mutationApplied
        rollbackAttempted = $rollbackAttempted
        rollbackSucceeded = $rollbackSucceeded
        backupCreated = $backupCreated
        tasks = $taskEvidence
        privacy = [ordered]@{
            containsTaskArguments = $false
            containsTaskXml = $false
            containsAudio = $false
            containsTranscript = $false
            containsSecrets = $false
        }
    }
    Write-MigrationEvidence -Evidence $evidence
}

if ($status -eq "no-go") { exit 1 }
