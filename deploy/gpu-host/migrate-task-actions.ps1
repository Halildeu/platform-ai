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
$script:SecurityInformation = 15
$script:SystemSid = "S-1-5-18"
$script:AdministratorsSid = "S-1-5-32-544"
$script:Tasks = @("platform-ai-live-stt", "platform-ai-meeting-ai")
$script:TransactionSchemaVersion = 1
$script:ActiveTransactionFile = "active-transaction.json"
$script:TransactionFile = "transaction.json"
$script:MigrationMutexName = "Global\platform-ai-task-action-migration-v1"
$script:MigrationMutex = $null
$script:MigrationLockTaken = $false

function Enable-SeSecurityPrivilege {
    if (-not ("PlatformAi.TokenPrivilege" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace PlatformAi {
    public static class TokenPrivilege {
        [StructLayout(LayoutKind.Sequential)]
        private struct Luid {
            public uint LowPart;
            public int HighPart;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TokenPrivileges {
            public uint PrivilegeCount;
            public Luid Luid;
            public uint Attributes;
        }

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(
            IntPtr processHandle, uint desiredAccess, out IntPtr tokenHandle
        );

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool LookupPrivilegeValue(
            string systemName, string name, out Luid luid
        );

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool AdjustTokenPrivileges(
            IntPtr tokenHandle,
            bool disableAllPrivileges,
            ref TokenPrivileges newState,
            uint bufferLength,
            IntPtr previousState,
            IntPtr returnLength
        );

        public static void Enable(string name) {
            const uint TokenAdjustPrivileges = 0x20;
            const uint TokenQuery = 0x8;
            const uint PrivilegeEnabled = 0x2;
            IntPtr token;
            if (!OpenProcessToken(
                GetCurrentProcess(), TokenAdjustPrivileges | TokenQuery, out token
            )) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            try {
                Luid luid;
                if (!LookupPrivilegeValue(null, name, out luid)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                TokenPrivileges privileges = new TokenPrivileges {
                    PrivilegeCount = 1,
                    Luid = luid,
                    Attributes = PrivilegeEnabled
                };
                if (!AdjustTokenPrivileges(
                    token, false, ref privileges, 0, IntPtr.Zero, IntPtr.Zero
                )) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                int error = Marshal.GetLastWin32Error();
                if (error != 0) { throw new Win32Exception(error); }
            } finally {
                CloseHandle(token);
            }
        }
    }
}
"@
    }
    try {
        [PlatformAi.TokenPrivilege]::Enable("SeSecurityPrivilege")
    } catch {
        throw "PRIVILEGE_NOT_HELD"
    }
}

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

function Convert-MigrationIdentityToSid {
    param([Parameter(Mandatory = $true)][string]$Identity)

    try {
        return (New-Object Security.Principal.SecurityIdentifier($Identity)).Value
    } catch {
        return (New-Object Security.Principal.NTAccount($Identity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
}

function Assert-MigrationAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) { throw "BACKUP_ACL_INVALID" }
    $ownerSid = Convert-MigrationIdentityToSid -Identity $acl.Owner
    if ($ownerSid -notin @($script:SystemSid, $script:AdministratorsSid)) {
        throw "BACKUP_ACL_INVALID"
    }
    $allowed = @($script:SystemSid, $script:AdministratorsSid)
    $seen = @{}
    foreach ($rule in @($acl.Access)) {
        $sid = Convert-MigrationIdentityToSid -Identity `
            $rule.IdentityReference.Value
        if ($rule.IsInherited -or $allowed -notcontains $sid -or
            $rule.AccessControlType -ne `
                [Security.AccessControl.AccessControlType]::Allow) {
            throw "BACKUP_ACL_INVALID"
        }
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
            throw "BACKUP_ACL_INVALID"
        }
        $seen[$sid] = $true
    }
    foreach ($sid in $allowed) {
        if (-not $seen.ContainsKey($sid)) { throw "BACKUP_ACL_INVALID" }
    }
}

function Assert-MigrationPathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($fullRoot, `
        [StringComparison]::OrdinalIgnoreCase)) {
        throw "BACKUP_PATH_INVALID"
    }
    return $fullPath
}

function Write-DurableText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Value)
    $stream = New-Object IO.FileStream(
        $Path,
        [IO.FileMode]::Create,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None,
        4096,
        [IO.FileOptions]::WriteThrough
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function Write-DurableFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    Write-DurableText -Path $Path -Value $Value
    Set-Acl -LiteralPath $Path -AclObject (New-MigrationAcl)
    Assert-MigrationAcl -Path $Path
}

function Write-DurableJsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $directory = Split-Path -Parent ([IO.Path]::GetFullPath($Path))
    $tempPath = Join-Path $directory (
        ".{0}.{1}.tmp" -f [IO.Path]::GetFileName($Path),
        [Guid]::NewGuid().ToString("N")
    )
    $rollbackPath = Join-Path $directory (
        ".{0}.{1}.rollback" -f [IO.Path]::GetFileName($Path),
        [Guid]::NewGuid().ToString("N")
    )
    try {
        $json = $Value | ConvertTo-Json -Depth 10
        Write-DurableFile -Path $tempPath `
            -Value ($json + [Environment]::NewLine)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            Assert-MigrationAcl -Path $Path
            [IO.File]::Replace($tempPath, $Path, $rollbackPath, $true)
        } else {
            [IO.File]::Move($tempPath, $Path)
        }
        Set-Acl -LiteralPath $Path -AclObject (New-MigrationAcl)
        Assert-MigrationAcl -Path $Path
    } finally {
        foreach ($candidate in @($tempPath, $rollbackPath)) {
            if (Test-Path -LiteralPath $candidate) {
                Remove-Item -LiteralPath $candidate -Force `
                    -ErrorAction SilentlyContinue
            }
        }
    }
}

function Initialize-MigrationRoot {
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
    Assert-MigrationAcl -Path $fullRoot -Directory
    return $fullRoot
}

function New-BackupDirectory {
    param([Parameter(Mandatory = $true)][string]$Root)

    $fullRoot = Initialize-MigrationRoot -Root $Root

    $batch = Join-Path $fullRoot (
        "{0}-{1}" -f [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"),
        [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $batch -Force | Out-Null
    Set-Acl -LiteralPath $batch -AclObject (New-MigrationAcl -Directory)
    Assert-MigrationAcl -Path $batch -Directory
    return $batch
}

function Write-BackupFile {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][ValidateSet("xml", "sddl")]
        [string]$Extension,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $path = Join-Path $Directory ("{0}.{1}" -f $TaskName, $Extension)
    Write-DurableFile -Path $path -Value $Value
    return $path
}

function Write-MigrationTransaction {
    param(
        [Parameter(Mandatory = $true)]$Transaction,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $batchPath = Assert-MigrationPathUnderRoot `
        -Path ([string]$Transaction.batchDirectory) -Root $Root
    Assert-MigrationAcl -Path $batchPath -Directory
    Write-DurableJsonAtomic -Path (Join-Path $batchPath $script:TransactionFile) `
        -Value $Transaction
    Write-DurableJsonAtomic -Path (Join-Path $Root $script:ActiveTransactionFile) `
        -Value $Transaction
}

function Read-MigrationJson {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-MigrationAcl -Path $Path
    try {
        return ([IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) |
            ConvertFrom-Json -ErrorAction Stop)
    } catch {
        throw "TRANSACTION_INVALID"
    }
}

function Read-ActiveMigrationTransaction {
    param([Parameter(Mandatory = $true)][string]$Root)

    $fullRoot = [IO.Path]::GetFullPath($Root)
    if (-not (Test-Path -LiteralPath $fullRoot -PathType Container)) {
        return $null
    }
    if ((Get-Item -LiteralPath $fullRoot -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw "BACKUP_PATH_INVALID"
    }
    Assert-MigrationAcl -Path $fullRoot -Directory
    $activePath = Join-Path $fullRoot $script:ActiveTransactionFile
    if (-not (Test-Path -LiteralPath $activePath -PathType Leaf)) {
        return $null
    }
    $transaction = Read-MigrationJson -Path $activePath
    if ([int]$transaction.schemaVersion -ne $script:TransactionSchemaVersion -or
        [string]$transaction.transactionId -notmatch '^[0-9a-f]{32}$' -or
        [string]$transaction.phase -notin @(
            "prepared", "first-applied", "second-applied", "committed",
            "rolled-back", "recovered"
        ) -or @($transaction.tasks).Count -ne $script:Tasks.Count) {
        throw "TRANSACTION_INVALID"
    }
    $batchPath = Assert-MigrationPathUnderRoot `
        -Path ([string]$transaction.batchDirectory) -Root $fullRoot
    if (-not (Test-Path -LiteralPath $batchPath -PathType Container) -or
        (Get-Item -LiteralPath $batchPath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) {
        throw "TRANSACTION_INVALID"
    }
    Assert-MigrationAcl -Path $batchPath -Directory
    $batchTransaction = Read-MigrationJson `
        -Path (Join-Path $batchPath $script:TransactionFile)
    if ([string]$batchTransaction.transactionId -ne
        [string]$transaction.transactionId) {
        throw "TRANSACTION_INVALID"
    }
    return $transaction
}

function Remove-ActiveMigrationTransaction {
    param([Parameter(Mandatory = $true)][string]$Root)

    $activePath = Join-Path ([IO.Path]::GetFullPath($Root)) `
        $script:ActiveTransactionFile
    if (Test-Path -LiteralPath $activePath -PathType Leaf) {
        Assert-MigrationAcl -Path $activePath
        Remove-Item -LiteralPath $activePath -Force
    }
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

function Get-ProcessIdentityProof {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][int[]]$ExpectedAncestorPids
    )

    $chain = @()
    $currentId = $ProcessId
    $ancestorMatched = $false
    for ($depth = 0; $depth -lt 8 -and $currentId -gt 0; $depth++) {
        $process = Get-WmiObject -Class Win32_Process `
            -Filter ("ProcessId = {0}" -f $currentId) -ErrorAction Stop
        if ($null -eq $process -or
            [string]::IsNullOrWhiteSpace([string]$process.ExecutablePath) -or
            $null -eq $process.CreationDate) {
            throw "TASK_LISTENER_INVALID"
        }
        $parentId = [int]$process.ParentProcessId
        $identityValue = "{0}|{1}|{2}" -f (
            [string]$process.ExecutablePath
        ).ToLowerInvariant(), ([string]$process.CreationDate), $parentId
        $chain += [ordered]@{
            pid = $currentId
            parentPid = $parentId
            identitySha256 = Get-StringSha256 -Value $identityValue
        }
        if ($ExpectedAncestorPids -contains $currentId) {
            $ancestorMatched = $true
            break
        }
        if ($parentId -eq $currentId -or $parentId -le 4) { break }
        $currentId = $parentId
    }
    if ($chain.Count -eq 0 -or -not $ancestorMatched) {
        throw "TASK_LISTENER_INVALID"
    }
    $chainJson = $chain | ConvertTo-Json -Depth 4 -Compress
    return [pscustomobject]@{
        Pid = [int]$chain[0].pid
        ParentPids = @($chain | ForEach-Object { [int]$_.parentPid })
        Chain = $chain
        ChainHash = Get-StringSha256 -Value $chainJson
    }
}

function Get-ListenerProcessProof {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int[]]$TaskPids
    )

    try {
        $listenerPids = @(Get-NetTCPConnection -State Listen -LocalPort $Port `
            -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        throw "TASK_LISTENER_INVALID"
    }
    if ($listenerPids.Count -ne 1 -or [int]$listenerPids[0] -le 0) {
        throw "TASK_LISTENER_INVALID"
    }
    return Get-ProcessIdentityProof -ProcessId ([int]$listenerPids[0]) `
        -ExpectedAncestorPids $TaskPids
}

function Test-ProcessProofEqual {
    param($Left, $Right)

    return $null -ne $Left -and $null -ne $Right -and
        [int]$Left.Pid -eq [int]$Right.Pid -and
        [string]$Left.ChainHash -eq [string]$Right.ChainHash
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
    $spec = Get-GpuHostTaskSpec -TaskName $TaskName
    $listener = Get-ListenerProcessProof -Port ([int]$spec.Port) -TaskPids $pids
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
        Listener = $listener
        Port = [int]$spec.Port
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

    Assert-MigrationAcl -Path ([string]$Snapshot.XmlPath)
    Assert-MigrationAcl -Path ([string]$Snapshot.SddlPath)
    $xml = [IO.File]::ReadAllText(
        [string]$Snapshot.XmlPath, [Text.Encoding]::UTF8
    )
    $sddl = [IO.File]::ReadAllText(
        [string]$Snapshot.SddlPath, [Text.Encoding]::UTF8
    )
    if ((Get-StringSha256 -Value $xml) -ne [string]$Snapshot.XmlHash -or
        (Get-StringSha256 -Value $sddl) -ne [string]$Snapshot.SddlHash) {
        throw "BACKUP_HASH_INVALID"
    }

    [void]$Folder.RegisterTask(
        $Snapshot.Name,
        $xml,
        $script:TaskUpdate,
        $Snapshot.PrincipalUser,
        $null,
        $Snapshot.LogonType,
        $sddl
    )
}

function Get-TransactionSnapshots {
    param(
        [Parameter(Mandatory = $true)]$Transaction,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $snapshots = @{}
    $batchPath = Assert-MigrationPathUnderRoot `
        -Path ([string]$Transaction.batchDirectory) -Root $Root
    foreach ($entry in @($Transaction.tasks)) {
        $name = [string]$entry.taskName
        if ($script:Tasks -notcontains $name -or $snapshots.ContainsKey($name)) {
            throw "TRANSACTION_INVALID"
        }
        $xmlPath = Assert-MigrationPathUnderRoot `
            -Path (Join-Path $batchPath ([string]$entry.xmlFileName)) -Root $Root
        $sddlPath = Assert-MigrationPathUnderRoot `
            -Path (Join-Path $batchPath ([string]$entry.sddlFileName)) -Root $Root
        foreach ($path in @($xmlPath, $sddlPath)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                (Get-Item -LiteralPath $path -Force).Attributes -band
                    [IO.FileAttributes]::ReparsePoint) {
                throw "TRANSACTION_INVALID"
            }
            Assert-MigrationAcl -Path $path
        }
        $snapshots[$name] = [pscustomobject]@{
            Name = $name
            XmlPath = $xmlPath
            SddlPath = $sddlPath
            XmlHash = [string]$entry.xmlSha256
            SddlHash = [string]$entry.sddlSha256
            InvariantHash = [string]$entry.invariantSha256
            PrincipalUser = [string]$entry.principalUser
            LogonType = [int]$entry.logonType
            Pids = @($entry.runningPids | ForEach-Object { [int]$_ })
            ListenerPid = [int]$entry.listenerPid
            ListenerIdentitySha256 = [string]$entry.listenerIdentitySha256
        }
    }
    if ($snapshots.Count -ne $script:Tasks.Count) { throw "TRANSACTION_INVALID" }
    return $snapshots
}

function Restore-MigrationTransaction {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)]$Transaction,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $snapshots = Get-TransactionSnapshots -Transaction $Transaction -Root $Root
    foreach ($taskName in $script:Tasks) {
        Restore-TaskSnapshot -Folder $Folder -Snapshot $snapshots[$taskName]
    }
    $restored = @{}
    foreach ($taskName in $script:Tasks) {
        $expected = $snapshots[$taskName]
        $actual = Get-TaskSnapshot -Folder $Folder -TaskName $taskName
        if ($actual.XmlHash -ne $expected.XmlHash -or
            $actual.InvariantHash -ne $expected.InvariantHash -or
            $actual.SddlHash -ne $expected.SddlHash) {
            throw "ROLLBACK_FAILED"
        }
        $restored[$taskName] = $actual
    }
    return $restored
}

function New-MigrationTransaction {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Snapshots,
        [Parameter(Mandatory = $true)][string]$BatchDirectory
    )

    $entries = @()
    foreach ($taskName in $script:Tasks) {
        $snapshot = $Snapshots[$taskName]
        $xmlPath = Write-BackupFile -Directory $BatchDirectory `
            -TaskName $taskName -Extension "xml" -Value $snapshot.Xml
        $sddlPath = Write-BackupFile -Directory $BatchDirectory `
            -TaskName $taskName -Extension "sddl" -Value $snapshot.Sddl
        if ((Get-StringSha256 -Value ([IO.File]::ReadAllText(
            $xmlPath, [Text.Encoding]::UTF8
        ))) -ne $snapshot.XmlHash -or
            (Get-StringSha256 -Value ([IO.File]::ReadAllText(
                $sddlPath, [Text.Encoding]::UTF8
            ))) -ne $snapshot.SddlHash) {
            throw "BACKUP_HASH_INVALID"
        }
        $entries += [ordered]@{
            taskName = $taskName
            xmlFileName = [IO.Path]::GetFileName($xmlPath)
            xmlSha256 = $snapshot.XmlHash
            sddlFileName = [IO.Path]::GetFileName($sddlPath)
            sddlSha256 = $snapshot.SddlHash
            invariantSha256 = $snapshot.InvariantHash
            principalUser = $snapshot.PrincipalUser
            logonType = $snapshot.LogonType
            runningPids = @($snapshot.Pids)
            listenerPid = [int]$snapshot.Listener.Pid
            listenerIdentitySha256 = [string]$snapshot.Listener.ChainHash
        }
    }
    return [ordered]@{
        schemaVersion = $script:TransactionSchemaVersion
        transactionId = [Guid]::NewGuid().ToString("N")
        createdUtc = [DateTime]::UtcNow.ToString("o")
        updatedUtc = [DateTime]::UtcNow.ToString("o")
        phase = "prepared"
        batchDirectory = $BatchDirectory
        appliedTasks = @()
        tasks = $entries
    }
}

function Set-MigrationTransactionPhase {
    param(
        [Parameter(Mandatory = $true)]$Transaction,
        [Parameter(Mandatory = $true)][string]$Phase,
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$AppliedTask = ""
    )

    if (-not [string]::IsNullOrWhiteSpace($AppliedTask)) {
        $Transaction.appliedTasks = @($Transaction.appliedTasks) + $AppliedTask
    }
    $Transaction.phase = $Phase
    $Transaction.updatedUtc = [DateTime]::UtcNow.ToString("o")
    Write-MigrationTransaction -Transaction $Transaction -Root $Root
}

function Test-GitHubHostedCiFixture {
    param(
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)]$Folder
    )

    if ($env:GITHUB_ACTIONS -ne "true" -or
        $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
        [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP) -or
        [string]$Identity.Name -notmatch '\\runneradmin$') {
        return $false
    }
    try {
        [void](Assert-MigrationPathUnderRoot -Path $BackupRoot `
            -Root $env:RUNNER_TEMP)
        if ([string]::IsNullOrWhiteSpace($EvidencePath)) { return $false }
        [void](Assert-MigrationPathUnderRoot -Path $EvidencePath `
            -Root $env:RUNNER_TEMP)
        foreach ($taskName in $script:Tasks) {
            $task = $Folder.GetTask($taskName)
            if ([string]$task.Definition.RegistrationInfo.Description -ne
                "platform-ai CI migration contract") {
                return $false
            }
        }
    } catch {
        return $false
    }
    return $true
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
    $afterListenerPid = $null
    $afterListenerIdentity = $null
    $afterListenerParentPids = @()
    $sameProcess = $false
    $sameListenerProcess = $false
    if ($null -ne $After) {
        $afterRepoClass = $After.Contract.RepoClass
        $afterInvariantHash = $After.InvariantHash
        $afterSddlHash = $After.SddlHash
        $afterPids = @($After.Pids)
        $sameProcess = Test-IntArrayEqual -Left $Before.Pids -Right $After.Pids
        $afterListenerPid = [int]$After.Listener.Pid
        $afterListenerIdentity = [string]$After.Listener.ChainHash
        $afterListenerParentPids = @($After.Listener.ParentPids)
        $sameListenerProcess = Test-ProcessProofEqual `
            -Left $Before.Listener -Right $After.Listener
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
        listenerPidBefore = [int]$Before.Listener.Pid
        listenerPidAfter = $afterListenerPid
        listenerParentPidsBefore = @($Before.Listener.ParentPids)
        listenerParentPidsAfter = $afterListenerParentPids
        listenerIdentityBeforeSha256 = [string]$Before.Listener.ChainHash
        listenerIdentityAfterSha256 = $afterListenerIdentity
        sameListenerProcess = $sameListenerProcess
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
    [Console]::Out.WriteLine((
        "{0}{1}" -f $script:MigrationMarker, $encoded
    ))
}

function Get-FailureClass {
    param([Parameter(Mandatory = $true)][string]$Message)

    foreach ($known in @(
        "IDENTITY_NOT_APPROVED", "PRIVILEGE_NOT_HELD", "REPO_ROOT_INVALID",
        "BACKUP_PATH_INVALID", "BACKUP_ACL_INVALID", "BACKUP_HASH_INVALID",
        "TRANSACTION_INVALID", "TASK_MISSING", "TASK_ACTION_COUNT_INVALID",
        "TASK_PRINCIPAL_INVALID", "TASK_ACTION_UNRECOGNIZED",
        "TASK_PROCESS_NOT_STABLE", "TASK_LISTENER_INVALID",
        "TASK_READBACK_INVALID",
        "TASK_INVARIANT_CHANGED", "TASK_SECURITY_CHANGED",
        "TASK_PROCESS_CHANGED", "MIGRATION_ALREADY_RUNNING",
        "TEST_INJECTED_FAILURE", "ROLLBACK_FAILED"
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
$transaction = $null
$transactionRecovered = $false
$isCiFixture = $false

try {
    $script:MigrationMutex = New-Object Threading.Mutex(
        $false, $script:MigrationMutexName
    )
    try {
        $script:MigrationLockTaken = $script:MigrationMutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $script:MigrationLockTaken = $true
        Write-Warning (
            "Previous migration process abandoned its lock; " +
            "durable transaction state will be reconciled before mutation."
        )
    }
    if (-not $script:MigrationLockTaken) {
        throw "MIGRATION_ALREADY_RUNNING"
    }
    if ($RepoRoot -ine $script:CanonicalRepoRoot) {
        throw "REPO_ROOT_INVALID"
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdministrator = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdministrator) { throw "IDENTITY_NOT_APPROVED" }
    Enable-SeSecurityPrivilege

    $contractPath = Join-Path $PSScriptRoot "task-action-contract.ps1"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "TASK_ACTION_UNRECOGNIZED"
    }
    . $contractPath

    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    $isCiFixture = Test-GitHubHostedCiFixture -Identity $identity -Folder $folder
    if (-not $isCiFixture -and $identity.Name -notmatch '\\denetimpc$') {
        throw "IDENTITY_NOT_APPROVED"
    }
    if (-not $isCiFixture -and $BackupRoot -ine $script:DefaultBackupRoot) {
        throw "BACKUP_ROOT_INVALID"
    }

    $activeTransaction = Read-ActiveMigrationTransaction -Root $BackupRoot
    if ($null -ne $activeTransaction -and
        [string]$activeTransaction.phase -in @("committed", "rolled-back", "recovered")) {
        Remove-ActiveMigrationTransaction -Root $BackupRoot
        $activeTransaction = $null
    }
    if ($null -ne $activeTransaction) {
        $rollbackAttempted = $true
        $restoredSnapshots = Restore-MigrationTransaction -Folder $folder `
            -Transaction $activeTransaction -Root $BackupRoot
        foreach ($taskName in $script:Tasks) {
            $before[$taskName] = $restoredSnapshots[$taskName]
            $after[$taskName] = $restoredSnapshots[$taskName]
            $changed[$taskName] = $false
        }
        $transaction = $activeTransaction
        Set-MigrationTransactionPhase -Transaction $transaction -Phase "recovered" `
            -Root $BackupRoot
        Remove-ActiveMigrationTransaction -Root $BackupRoot
        $rollbackSucceeded = $true
        $transactionRecovered = $true
        $failureClass = "interrupted-transaction-recovered"
        $status = "recovered"
    } else {
        foreach ($taskName in $script:Tasks) {
            $before[$taskName] = Get-TaskSnapshot -Folder $folder -TaskName $taskName
            $changed[$taskName] = `
                $before[$taskName].Contract.RepoClass -ne "canonical-repo"
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
            $backupDirectory = New-BackupDirectory -Root $BackupRoot
            $transaction = New-MigrationTransaction -Snapshots $before `
                -BatchDirectory $backupDirectory
            Write-MigrationTransaction -Transaction $transaction -Root $BackupRoot
            $backupCreated = $true

            $mutationIndex = 0
            foreach ($taskName in $script:Tasks) {
                if (-not $changed[$taskName]) { continue }
                $snapshot = $before[$taskName]
                $snapshot.Action.Path = "powershell.exe"
                $snapshot.Action.Arguments = $snapshot.Contract.CanonicalArguments
                $snapshot.Action.WorkingDirectory = ""
                Register-TaskDefinitionPreservingSecurity `
                    -Folder $folder -Snapshot $snapshot
                $mutationApplied = $true
                $mutationIndex += 1
                $phase = "first-applied"
                if ($mutationIndex -gt 1) { $phase = "second-applied" }
                Set-MigrationTransactionPhase -Transaction $transaction `
                    -Phase $phase -Root $BackupRoot -AppliedTask $taskName
                if ($isCiFixture -and $mutationIndex -eq 1 -and
                    $env:PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST -eq "1") {
                    [Environment]::Exit(9)
                }
                if ($isCiFixture -and $mutationIndex -eq 1 -and
                    $env:PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST -eq "1") {
                    throw "TEST_INJECTED_FAILURE"
                }
            }

            foreach ($taskName in $script:Tasks) {
                $after[$taskName] = Get-TaskSnapshot -Folder $folder -TaskName $taskName
                if ($after[$taskName].Contract.RepoClass -ne "canonical-repo") {
                    throw "TASK_READBACK_INVALID"
                }
                if ($after[$taskName].InvariantHash -ne `
                    $before[$taskName].InvariantHash) {
                    throw "TASK_INVARIANT_CHANGED"
                }
                if ($after[$taskName].SddlHash -ne $before[$taskName].SddlHash) {
                    throw "TASK_SECURITY_CHANGED"
                }
                if (-not (Test-IntArrayEqual -Left $before[$taskName].Pids `
                    -Right $after[$taskName].Pids) -or
                    -not (Test-ProcessProofEqual `
                        -Left $before[$taskName].Listener `
                        -Right $after[$taskName].Listener)) {
                    throw "TASK_PROCESS_CHANGED"
                }
            }
            Set-MigrationTransactionPhase -Transaction $transaction `
                -Phase "committed" -Root $BackupRoot
            Remove-ActiveMigrationTransaction -Root $BackupRoot
            $status = "go"
        }
    }
} catch {
    $failureClass = Get-FailureClass -Message $_.Exception.Message
    if ($mutationApplied -and $null -ne $transaction) {
        $rollbackAttempted = $true
        try {
            $restoredSnapshots = Restore-MigrationTransaction -Folder $folder `
                -Transaction $transaction -Root $BackupRoot
            foreach ($taskName in $script:Tasks) {
                $after[$taskName] = $restoredSnapshots[$taskName]
            }
            Set-MigrationTransactionPhase -Transaction $transaction `
                -Phase "rolled-back" -Root $BackupRoot
            Remove-ActiveMigrationTransaction -Root $BackupRoot
            $rollbackSucceeded = $true
        } catch {
            $failureClass = "rollback-failed"
            $rollbackSucceeded = $false
        }
    }
} finally {
    try {
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
            transactionRecovered = $transactionRecovered
            transactionPhase = $(if ($null -ne $transaction) {
                [string]$transaction.phase
            } else { $null })
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
    } finally {
        if ($null -ne $script:MigrationMutex) {
            if ($script:MigrationLockTaken) {
                [void]$script:MigrationMutex.ReleaseMutex()
            }
            $script:MigrationMutex.Dispose()
            $script:MigrationMutex = $null
            $script:MigrationLockTaken = $false
        }
    }
}

if ($status -eq "no-go") { exit 1 }
if ($status -eq "recovered") { exit 2 }
