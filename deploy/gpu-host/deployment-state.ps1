# Shared immutable deployment-state contract for the Windows GPU host.
# Dot-source this file from update.ps1 and drift-guard.ps1.

$script:DeploymentStateSchemaVersion = 1
$script:DeploymentStateSystemSid = "S-1-5-18"
$script:DeploymentStateAdministratorsSid = "S-1-5-32-544"
$script:DeploymentStateCommitPattern = '^[0-9a-f]{40}$'

function New-DeploymentStateAcl {
    param([switch]$Directory)

    $acl = New-Object Security.AccessControl.DirectorySecurity
    if (-not $Directory) {
        $acl = New-Object Security.AccessControl.FileSecurity
    }
    $acl.SetAccessRuleProtection($true, $false)

    $system = New-Object Security.Principal.SecurityIdentifier(
        $script:DeploymentStateSystemSid
    )
    $administrators = New-Object Security.Principal.SecurityIdentifier(
        $script:DeploymentStateAdministratorsSid
    )
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) {
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $rights = [Security.AccessControl.FileSystemRights]::FullControl

    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $system, $rights, $inheritance, $propagation, $allow
    )))
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $administrators, $rights, $inheritance, $propagation, $allow
    )))
    $acl.SetOwner($administrators)
    return $acl
}

function Convert-IdentityToSid {
    param([Parameter(Mandatory = $true)][string]$Identity)

    try {
        return (New-Object Security.Principal.SecurityIdentifier($Identity)).Value
    } catch {
        return (New-Object Security.Principal.NTAccount($Identity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
}

function Assert-DeploymentStateAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) {
        throw "Deployment state ACL inheritance must be disabled: $Path"
    }

    $ownerSid = Convert-IdentityToSid -Identity $acl.Owner
    if ($ownerSid -ne $script:DeploymentStateAdministratorsSid -and
        $ownerSid -ne $script:DeploymentStateSystemSid) {
        throw "Deployment state owner must be SYSTEM or Administrators: $Path"
    }

    $allowedSids = @(
        $script:DeploymentStateSystemSid,
        $script:DeploymentStateAdministratorsSid
    )
    $seen = @{}
    foreach ($rule in @($acl.Access)) {
        if ($rule.IsInherited) {
            throw "Deployment state ACL contains an inherited rule: $Path"
        }
        if ($rule.AccessControlType -ne
            [Security.AccessControl.AccessControlType]::Allow) {
            throw "Deployment state ACL contains a deny rule: $Path"
        }
        $sid = Convert-IdentityToSid -Identity $rule.IdentityReference.Value
        if ($allowedSids -notcontains $sid) {
            throw "Deployment state ACL grants an unexpected principal: $Path"
        }
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
            throw "Deployment state ACL principal lacks FullControl: $Path"
        }
        $seen[$sid] = $true
    }
    foreach ($sid in $allowedSids) {
        if (-not $seen.ContainsKey($sid)) {
            throw "Deployment state ACL is missing required principal ${sid}: $Path"
        }
    }
}

function Initialize-DeploymentStateRoot {
    param([Parameter(Mandatory = $true)][string]$StatePath)

    $fullPath = [IO.Path]::GetFullPath($StatePath)
    if ($fullPath.StartsWith("\\", [StringComparison]::Ordinal)) {
        throw "Deployment state path must be on a local volume."
    }
    $directory = Split-Path -Parent $fullPath
    if ([string]::IsNullOrWhiteSpace($directory)) {
        throw "Deployment state path must have an absolute parent directory."
    }

    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force -ErrorAction Stop |
            Out-Null
        Set-Acl -LiteralPath $directory -AclObject `
            (New-DeploymentStateAcl -Directory) -ErrorAction Stop
    }
    if ((Get-Item -LiteralPath $directory -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw "Deployment state directory must not be a reparse point."
    }
    Assert-DeploymentStateAcl -Path $directory -Directory
    return $fullPath
}

function Assert-DeploymentStateCommit {
    param(
        [AllowNull()][string]$Commit,
        [Parameter(Mandatory = $true)][string]$Field,
        [switch]$AllowNull
    )

    if ([string]::IsNullOrWhiteSpace($Commit)) {
        if ($AllowNull) { return $null }
        throw "Deployment state $Field must be a full 40-hex commit."
    }
    $normalized = $Commit.ToLowerInvariant()
    if ($normalized -notmatch $script:DeploymentStateCommitPattern) {
        throw "Deployment state $Field must be a full 40-hex commit."
    }
    return $normalized
}

function Read-DeploymentState {
    param([Parameter(Mandatory = $true)][string]$StatePath)

    $fullPath = [IO.Path]::GetFullPath($StatePath)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        return $null
    }
    if ((Get-Item -LiteralPath $fullPath -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw "Deployment state file must not be a reparse point."
    }
    Assert-DeploymentStateAcl -Path (Split-Path -Parent $fullPath) -Directory
    Assert-DeploymentStateAcl -Path $fullPath

    $raw = [IO.File]::ReadAllText($fullPath, [Text.Encoding]::UTF8)
    try {
        $state = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Deployment state JSON is malformed."
    }

    $required = @(
        "schemaVersion", "currentCommit", "previousCommit", "timestampUtc",
        "branchRef", "lastAction", "lastResult", "host"
    )
    foreach ($name in $required) {
        if ($state.PSObject.Properties.Name -notcontains $name) {
            throw "Deployment state is missing '$name'."
        }
    }
    if ([int]$state.schemaVersion -ne $script:DeploymentStateSchemaVersion) {
        throw "Unsupported deployment state schema version."
    }
    $state.currentCommit = Assert-DeploymentStateCommit `
        -Commit ([string]$state.currentCommit) -Field "currentCommit"
    $state.previousCommit = Assert-DeploymentStateCommit `
        -Commit ([string]$state.previousCommit) -Field "previousCommit" -AllowNull
    if ([string]$state.branchRef -notmatch '^refs/remotes/origin/[A-Za-z0-9._/-]+$') {
        throw "Deployment state branchRef is invalid."
    }
    if ([string]$state.lastAction -notin @("deploy", "rollback")) {
        throw "Deployment state lastAction is invalid."
    }
    if ([string]::IsNullOrWhiteSpace([string]$state.lastResult)) {
        throw "Deployment state lastResult is empty."
    }
    try {
        [void][DateTime]::Parse(
            [string]$state.timestampUtc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    } catch {
        throw "Deployment state timestampUtc is invalid."
    }
    return $state
}

function New-DeploymentStateRecord {
    param(
        [Parameter(Mandatory = $true)][string]$CurrentCommit,
        [AllowNull()][string]$PreviousCommit,
        [Parameter(Mandatory = $true)][string]$BranchRef,
        [Parameter(Mandatory = $true)][ValidateSet("deploy", "rollback")]
        [string]$Action,
        [Parameter(Mandatory = $true)][string]$Result
    )

    $hostName = $env:COMPUTERNAME
    if ([string]::IsNullOrWhiteSpace($hostName)) {
        $hostName = [Net.Dns]::GetHostName()
    }
    return [ordered]@{
        schemaVersion = $script:DeploymentStateSchemaVersion
        currentCommit = (Assert-DeploymentStateCommit `
            -Commit $CurrentCommit -Field "currentCommit")
        previousCommit = (Assert-DeploymentStateCommit `
            -Commit $PreviousCommit -Field "previousCommit" -AllowNull)
        timestampUtc = [DateTime]::UtcNow.ToString("o")
        branchRef = $BranchRef
        lastAction = $Action
        lastResult = $Result
        host = $hostName
    }
}

function Write-DeploymentStateAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)]$State
    )

    $fullPath = Initialize-DeploymentStateRoot -StatePath $StatePath
    $directory = Split-Path -Parent $fullPath
    $tempPath = Join-Path $directory (
        ".{0}.{1}.tmp" -f [IO.Path]::GetFileName($fullPath),
        [Guid]::NewGuid().ToString("N")
    )
    $rollbackPath = Join-Path $directory (
        ".{0}.{1}.rollback" -f [IO.Path]::GetFileName($fullPath),
        [Guid]::NewGuid().ToString("N")
    )

    try {
        $json = $State | ConvertTo-Json -Depth 4
        [IO.File]::WriteAllText(
            $tempPath,
            $json + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false))
        )
        Set-Acl -LiteralPath $tempPath -AclObject (New-DeploymentStateAcl)
        Assert-DeploymentStateAcl -Path $tempPath

        if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
            Assert-DeploymentStateAcl -Path $fullPath
            [IO.File]::Replace($tempPath, $fullPath, $rollbackPath, $true)
        } else {
            [IO.File]::Move($tempPath, $fullPath)
        }
        Set-Acl -LiteralPath $fullPath -AclObject (New-DeploymentStateAcl)
        Assert-DeploymentStateAcl -Path $fullPath
        [void](Read-DeploymentState -StatePath $fullPath)
    } finally {
        foreach ($candidate in @($tempPath, $rollbackPath)) {
            if (Test-Path -LiteralPath $candidate) {
                Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            }
        }
    }
}
