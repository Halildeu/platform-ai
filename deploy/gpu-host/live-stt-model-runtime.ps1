# Hardened exact-revision model runtime shared by staging and the launcher.

Set-StrictMode -Version 2.0

$script:LiveSttModelPolicySchema = "platform-ai.live-stt.model-policy.v1"
$script:LiveSttModelManifestName = "integrity-manifest.json"

function Get-LiveSttDefaultModelRuntimeRoot {
    return Join-Path $env:ProgramData "Acik\platform-ai\models\live-stt"
}

function Test-LiveSttTrustedCiPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($env:CI -ne "true" -or $env:GITHUB_ACTIONS -ne "true" -or
        $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
        [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
        return $false
    }
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        $runnerRoot = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
        return (
            $identity -match '\\runneradmin$' -and
            $candidate.StartsWith(
                $runnerRoot + '\',
                [StringComparison]::OrdinalIgnoreCase
            )
        )
    } catch {
        return $false
    }
}

function Assert-LiveSttModelRootPath {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [switch]$AllowTrustedCiPath
    )

    if ($RuntimeRoot.StartsWith('\\') -or $RuntimeRoot.StartsWith('\\?\') -or
        $RuntimeRoot.StartsWith('\\.\')) {
        throw "Live STT model runtime root must be a local filesystem path."
    }
    $full = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    if ($full.Length -gt 240 -or
        ($full.Length -gt 2 -and $full.Substring(2).Contains(':'))) {
        throw "Live STT model runtime root path is unsupported."
    }
    $default = [IO.Path]::GetFullPath(
        (Get-LiveSttDefaultModelRuntimeRoot)
    ).TrimEnd('\')
    if (-not $full.Equals($default, [StringComparison]::OrdinalIgnoreCase) -and
        (-not $AllowTrustedCiPath -or -not (Test-LiveSttTrustedCiPath -Path $full))) {
        throw "Live STT model runtime root must use the fixed ProgramData path."
    }
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "Live STT model runtime root has no local volume."
    }
    $drive = New-Object IO.DriveInfo($root)
    if ($drive.DriveType -ne [IO.DriveType]::Fixed) {
        throw "Live STT model runtime root must reside on a fixed local volume."
    }
    $cursor = $full
    while ($cursor -and -not (Test-Path -LiteralPath $cursor)) {
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    while ($cursor -and (Test-Path -LiteralPath $cursor)) {
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Live STT model runtime root must not traverse a reparse point."
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $full
}

function Assert-LiveSttNoReparseTree {
    param([Parameter(Mandatory = $true)][string]$Path)

    $rootItem = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $pending = New-Object Collections.Generic.Queue[IO.FileSystemInfo]
    $pending.Enqueue($rootItem)
    while ($pending.Count -gt 0) {
        $item = $pending.Dequeue()
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Live STT model runtime must not contain a reparse point: $($item.FullName)"
        }
        if ($item -is [IO.DirectoryInfo]) {
            foreach ($child in $item.GetFileSystemInfos()) {
                $pending.Enqueue($child)
            }
        }
    }
}

function New-LiveSttModelAcl {
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

function Set-LiveSttModelTreeAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-LiveSttNoReparseTree -Path $Path
    $items = @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
    [Array]::Reverse($items)
    foreach ($item in $items) {
        $acl = New-LiveSttModelAcl -Directory:($item -is [IO.DirectoryInfo])
        Set-Acl -LiteralPath $item.FullName -AclObject $acl -ErrorAction Stop
    }
    Set-Acl -LiteralPath $Path -AclObject (New-LiveSttModelAcl -Directory) `
        -ErrorAction Stop
}

function Assert-LiveSttModelAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $ownerSid = try {
        (New-Object Security.Principal.NTAccount($acl.Owner)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch { $acl.Owner }
    if ($ownerSid -notin @("S-1-5-18", "S-1-5-32-544") -or
        -not $acl.AreAccessRulesProtected) {
        throw "Live STT model ACL owner/protection is invalid: $Path"
    }
    $rules = @($acl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    ))
    if ($rules.Count -ne 2) {
        throw "Live STT model ACL must contain exactly SYSTEM and Administrators."
    }
    $expected = @("S-1-5-18", "S-1-5-32-544")
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin $expected -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            (($rule.FileSystemRights -band `
                [Security.AccessControl.FileSystemRights]::FullControl) -ne `
                [Security.AccessControl.FileSystemRights]::FullControl)) {
            throw "Live STT model ACL contains an unauthorized rule: $Path"
        }
    }
}

function Assert-LiveSttModelTreeAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-LiveSttNoReparseTree -Path $Path
    Assert-LiveSttModelAcl -Path $Path -Directory
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force -Recurse `
            -ErrorAction Stop)) {
        Assert-LiveSttModelAcl -Path $item.FullName `
            -Directory:($item -is [IO.DirectoryInfo])
    }
}

function Read-LiveSttModelPolicy {
    param([Parameter(Mandatory = $true)][string]$PolicyPath)

    $raw = [IO.File]::ReadAllText($PolicyPath, (New-Object Text.UTF8Encoding($false, $true)))
    if ($raw.Length -gt 32768 -or $raw.StartsWith([char]0xFEFF)) {
        throw "Live STT model policy encoding or size is invalid."
    }
    $policy = $raw | ConvertFrom-Json -ErrorAction Stop
    if ($policy.schema -ne $script:LiveSttModelPolicySchema -or
        @($policy.models).Count -ne 2) {
        throw "Live STT model policy schema/model count is invalid."
    }
    $roles = @()
    foreach ($model in @($policy.models)) {
        $properties = @($model.PSObject.Properties.Name | Sort-Object)
        if (($properties -join ',') -ne `
            'modelBinSha256,relativePath,repository,revision,role' -or
            $model.role -notin @('live', 'final') -or
            $model.repository -notmatch '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' -or
            $model.revision -cnotmatch '^[0-9a-f]{40}$' -or
            $model.modelBinSha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $model.relativePath -cnotmatch `
                ('^artifacts/{0}/[0-9a-f]{{40}}$' -f $model.role) -or
            -not $model.relativePath.EndsWith($model.revision)) {
            throw "Live STT model policy entry is invalid."
        }
        $roles += $model.role
    }
    if ((@($roles | Sort-Object) -join ',') -ne 'final,live') {
        throw "Live STT model policy roles are invalid."
    }
    return $policy
}

function Invoke-LiveSttModelVerifier {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$HelperPath,
        [Parameter(Mandatory = $true)]$Model,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string]$DigestOutputPath = "",
        [ValidateRange(1, 3600)][int]$TimeoutSec = 600
    )

    $arguments = @(
        $HelperPath,
        "verify",
        "--repository", [string]$Model.repository,
        "--revision", [string]$Model.revision,
        "--model-bin-sha256", [string]$Model.modelBinSha256,
        "--destination", $Destination
    )
    if (-not [string]::IsNullOrWhiteSpace($DigestOutputPath)) {
        $arguments += @("--digest-output", $DigestOutputPath)
    }
    $result = Invoke-GpuHostBoundedProcess -FileName $PythonExe `
        -ArgumentList $arguments -TimeoutSec $TimeoutSec `
        -Operation ("Verify {0} live-STT model" -f $Model.role)
    if ($result.ExitCode -ne 0) {
        throw "Live STT $($Model.role) model integrity verification failed."
    }
}

function Assert-LiveSttModelSet {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$PolicyPath,
        [Parameter(Mandatory = $true)][string]$HelperPath,
        [switch]$AllowTrustedCiPath
    )

    $root = Assert-LiveSttModelRootPath -RuntimeRoot $RuntimeRoot `
        -AllowTrustedCiPath:$AllowTrustedCiPath
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Live STT model runtime root is missing."
    }
    $policy = Read-LiveSttModelPolicy -PolicyPath $PolicyPath
    Assert-LiveSttModelTreeAcl -Path $root
    foreach ($model in @($policy.models)) {
        $destination = Join-Path $root ([string]$model.relativePath).Replace('/', '\')
        if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
            throw "Live STT $($model.role) exact revision is not staged."
        }
        $digestOutput = Join-Path $root (
            ".{0}.{1}.tree-digest" -f $model.role, [Guid]::NewGuid().ToString("N")
        )
        try {
            Invoke-LiveSttModelVerifier -PythonExe $PythonExe -HelperPath $HelperPath `
                -Model $model -Destination $destination `
                -DigestOutputPath $digestOutput
            if (-not (Test-Path -LiteralPath $digestOutput -PathType Leaf)) {
                throw "Live STT $($model.role) model tree digest was not produced."
            }
            $treeDigest = [IO.File]::ReadAllText($digestOutput).Trim()
            if ($treeDigest -cnotmatch '^[0-9a-f]{64}$') {
                throw "Live STT $($model.role) model tree digest is invalid."
            }
            $model | Add-Member -NotePropertyName treeSha256 `
                -NotePropertyValue $treeDigest -Force
        } finally {
            if (Test-Path -LiteralPath $digestOutput) {
                Remove-Item -LiteralPath $digestOutput -Force `
                    -ErrorAction SilentlyContinue
            }
        }
    }
    return $policy
}
