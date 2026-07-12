<#
.SYNOPSIS
  Manages the test-only private gateway hosts entry used by meeting-ai app mTLS.

.DESCRIPTION
  Adds exactly one marked hosts-file block for the private test gateway while
  preserving unrelated content and the destination file ACL. The write uses a
  same-directory temporary file and File.Replace so a partial write cannot
  truncate the Windows hosts file.

  This is a TEST-ENVIRONMENT SHIM. Production name resolution belongs to
  split-horizon private DNS. The hosts entry is not a security control; server
  certificate SAN verification, CA pinning, client mTLS, and the WireGuard
  firewall boundary remain mandatory.

.PARAMETER TestHostShim
  Explicit acknowledgement that this script is only for the test GPU host.

.PARAMETER HostsPath
  Hosts file to manage. Defaults to the Windows system hosts file. A custom
  path is supported for the Windows CI behavior contract.

.PARAMETER Remove
  Removes only the managed block (and a legacy dedicated mapping from the old
  inline runbook, when present).

.PARAMETER RestoreBackup
  Restores the last pre-mutation backup produced by this script.

.EXAMPLE
  .\configure-private-gateway-host.ps1 -TestHostShim -Confirm:$false
#>
[CmdletBinding(
    SupportsShouldProcess = $true,
    ConfirmImpact = "High",
    DefaultParameterSetName = "Apply"
)]
param(
    [Parameter(Mandatory = $true)]
    [switch]$TestHostShim,

    [string]$GatewayHostname = "meeting-ai-gateway.internal",
    [string]$GatewayIPv4 = "10.99.0.1",
    [string]$HostsPath = "",

    [Parameter(ParameterSetName = "Remove", Mandatory = $true)]
    [switch]$Remove,

    [Parameter(ParameterSetName = "Restore", Mandatory = $true)]
    [switch]$RestoreBackup,

    [ValidateRange(0, 300)]
    [int]$MutexTimeoutSeconds = 30,

    # Test seams are accepted only with a non-system HostsPath. They let the
    # windows-latest contract prove flush/resolve behavior without mutating the
    # runner's real name resolution.
    [scriptblock]$DnsFlushAction,
    [scriptblock]$ResolverProbe
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$BeginMarker = "# BEGIN platform-ai meeting-ai private gateway test shim"
$EndMarker = "# END platform-ai meeting-ai private gateway test shim"
$MutexName = "Global\platform-ai-private-gateway-host-v1"
$Utf8NoBom = New-Object Text.UTF8Encoding($false, $true)

function Assert-CanonicalIPv4 {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value -notmatch '^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$') {
        throw "GatewayIPv4 must be a canonical IPv4 address without CIDR or leading zeroes."
    }
    $parts = $Value.Split('.')
    foreach ($part in $parts) {
        if ([int]$part -gt 255) {
            throw "GatewayIPv4 contains an octet greater than 255."
        }
    }
}

function Assert-CanonicalHostname {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Length -gt 253 -or $Value.IndexOf('.') -lt 1 -or $Value.EndsWith('.')) {
        throw "GatewayHostname must be a non-trailing-dot fully qualified hostname."
    }
    if ($Value -match '[\s#]' -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "GatewayHostname contains whitespace, a comment marker, or a newline."
    }
    foreach ($label in $Value.Split('.')) {
        if ($label -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$') {
            throw "GatewayHostname contains an invalid DNS label."
        }
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw "HostsPath must be an absolute local path."
    }
    if ($Path.StartsWith('\\') -or $Path.StartsWith('\\?\') -or $Path.StartsWith('\\.\')) {
        throw "HostsPath must not be a UNC or device path."
    }
    return [IO.Path]::GetFullPath($Path)
}

function Read-Utf8WithoutBom {
    param([Parameter(Mandatory = $true)][string]$Path)

    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "HostsPath must be UTF-8 without BOM. Refusing to rewrite a BOM-prefixed file."
    }
    return $Utf8NoBom.GetString($bytes)
}

function Remove-ManagedGatewayContent {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Hostname,
        [Parameter(Mandatory = $true)][string]$IPv4
    )

    $beginPattern = '(?m)^[\t ]*' + [Regex]::Escape($BeginMarker) + '[\t ]*\r?$'
    $endPattern = '(?m)^[\t ]*' + [Regex]::Escape($EndMarker) + '[\t ]*\r?$'
    $beginCount = [Regex]::Matches($Content, $beginPattern).Count
    $endCount = [Regex]::Matches($Content, $endPattern).Count
    if ($beginCount -ne $endCount -or $beginCount -gt 1) {
        throw "HostsPath contains duplicate or unbalanced platform-ai managed markers."
    }

    $base = $Content
    if ($beginCount -eq 1) {
        $blockPattern = '(?ms)^[\t ]*' + [Regex]::Escape($BeginMarker) +
            '[\t ]*\r?\n.*?^[\t ]*' + [Regex]::Escape($EndMarker) +
            '[\t ]*(?:\r?\n)?'
        $base = [Regex]::Replace($base, $blockPattern, '', 1)
        if ([Regex]::IsMatch($base, $beginPattern) -or [Regex]::IsMatch($base, $endPattern)) {
            throw "HostsPath managed block could not be parsed safely."
        }
    }

    # Preserve every unrelated line and its original line ending. The one
    # migration exception is the exact legacy line emitted by the old runbook.
    $builder = New-Object Text.StringBuilder
    $lineMatches = [Regex]::Matches($base, '(?m)^(?<line>[^\r\n]*)(?<eol>\r\n|\n|$)')
    foreach ($match in $lineMatches) {
        $line = $match.Groups['line'].Value
        $eol = $match.Groups['eol'].Value
        if ($line.Length -eq 0 -and $eol.Length -eq 0 -and $match.Index -eq $base.Length) {
            continue
        }

        $trimmed = $line.Trim()
        $skipLegacy = $false
        if ($trimmed.Length -gt 0 -and -not $trimmed.StartsWith('#')) {
            $commentIndex = $line.IndexOf('#')
            $code = if ($commentIndex -ge 0) { $line.Substring(0, $commentIndex) } else { $line }
            $code = $code.Trim()
            if ($code.Length -gt 0) {
                $tokens = @([Regex]::Split($code, '\s+') | Where-Object { $_.Length -gt 0 })
                if ($tokens.Count -ge 2) {
                    $aliases = @($tokens[1..($tokens.Count - 1)])
                    $ownsHostname = @($aliases | Where-Object {
                        $_.Equals($Hostname, [StringComparison]::OrdinalIgnoreCase)
                    }).Count -gt 0
                    if ($ownsHostname) {
                        if (-not $tokens[0].Equals($IPv4, [StringComparison]::Ordinal)) {
                            throw "GatewayHostname already has an active mapping outside the managed block."
                        }
                        if ($tokens.Count -ne 2) {
                            throw "GatewayHostname shares an unmanaged aliases line; refusing implicit migration."
                        }
                        $skipLegacy = $true
                    }
                }
            }
        }

        if (-not $skipLegacy) {
            [void]$builder.Append($line)
            [void]$builder.Append($eol)
        }
    }
    return $builder.ToString()
}

function New-ManagedGatewayContent {
    param(
        [Parameter(Mandatory = $true)][string]$BaseContent,
        [Parameter(Mandatory = $true)][string]$Hostname,
        [Parameter(Mandatory = $true)][string]$IPv4
    )

    $newline = if ($BaseContent.Contains("`r`n")) { "`r`n" } else { "`n" }
    if ($BaseContent.Length -eq 0) { $newline = "`r`n" }
    $prefix = $BaseContent
    if ($prefix.Length -gt 0 -and -not $prefix.EndsWith("`n")) {
        $prefix += $newline
    }
    $block = @(
        $BeginMarker,
        "# Test only. Production uses private DNS; mTLS SAN and CA validation remain mandatory.",
        ("{0} {1}" -f $IPv4, $Hostname),
        $EndMarker
    ) -join $newline
    return $prefix + $block + $newline
}

function Write-HostsFileAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)]$OriginalAcl,
        [string]$BackupPath = ""
    )

    $directory = Split-Path -Parent $Path
    $tempPath = Join-Path $directory (".{0}.platform-ai.{1}.tmp" -f
        [IO.Path]::GetFileName($Path), [Guid]::NewGuid().ToString('N'))
    $transientBackupPath = ""
    try {
        [IO.File]::WriteAllText($tempPath, $Content, $Utf8NoBom)
        Set-Acl -LiteralPath $tempPath -AclObject $OriginalAcl
        if ([string]::IsNullOrWhiteSpace($BackupPath)) {
            # Windows PowerShell 5.1 / .NET Framework rejects a null backup
            # path in the four-argument File.Replace overload. Keep the swap
            # atomic by using a same-directory transient backup and remove it
            # only after the destination postcondition has been established.
            $transientBackupPath = Join-Path $directory `
                (".{0}.platform-ai.{1}.rollback" -f
                    [IO.Path]::GetFileName($Path), [Guid]::NewGuid().ToString('N'))
            [IO.File]::Replace($tempPath, $Path, $transientBackupPath, $true)
        } else {
            [IO.File]::Replace($tempPath, $Path, $BackupPath, $true)
            Set-Acl -LiteralPath $BackupPath -AclObject $OriginalAcl
        }
        Set-Acl -LiteralPath $Path -AclObject $OriginalAcl
        if (-not [string]::IsNullOrWhiteSpace($transientBackupPath) -and
            (Test-Path -LiteralPath $transientBackupPath)) {
            Remove-Item -LiteralPath $transientBackupPath -Force
        }
    } finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force
        }
        if (-not [string]::IsNullOrWhiteSpace($transientBackupPath) -and
            (Test-Path -LiteralPath $transientBackupPath)) {
            Remove-Item -LiteralPath $transientBackupPath -Force
        }
    }
}

function Invoke-DnsFlush {
    param([scriptblock]$Action)

    if ($null -ne $Action) {
        $result = & $Action
        if ($null -ne $result -and [int]$result -ne 0) {
            throw "DNS flush test seam returned a non-zero result."
        }
        return
    }
    $process = Start-Process -FilePath "ipconfig.exe" -ArgumentList "/flushdns" `
        -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "ipconfig /flushdns failed with exit code $($process.ExitCode)."
    }
}

function Assert-GatewayResolution {
    param(
        [Parameter(Mandatory = $true)][string]$Hostname,
        [Parameter(Mandatory = $true)][string]$IPv4,
        [scriptblock]$Probe
    )

    if ($null -ne $Probe) {
        $addresses = @(& $Probe $Hostname | ForEach-Object { "$_" })
    } else {
        $addresses = @([Net.Dns]::GetHostAddresses($Hostname) |
            Where-Object { $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork } |
            ForEach-Object { $_.ToString() })
    }
    $unique = @($addresses | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique)
    if ($unique.Count -ne 1 -or $unique[0] -ne $IPv4) {
        throw "Private gateway DNS resolution did not return exactly the configured IPv4."
    }
}

if (-not $TestHostShim) {
    throw "-TestHostShim acknowledgement is required; production must use private DNS."
}
Assert-CanonicalIPv4 -Value $GatewayIPv4
Assert-CanonicalHostname -Value $GatewayHostname
$GatewayHostname = $GatewayHostname.ToLowerInvariant()

$defaultHostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
if ([string]::IsNullOrWhiteSpace($HostsPath)) { $HostsPath = $defaultHostsPath }
$HostsPath = Get-CanonicalPath -Path $HostsPath
$defaultHostsPath = Get-CanonicalPath -Path $defaultHostsPath
$isSystemHosts = $HostsPath.Equals($defaultHostsPath, [StringComparison]::OrdinalIgnoreCase)

if (($null -ne $DnsFlushAction -or $null -ne $ResolverProbe) -and $isSystemHosts) {
    throw "DNS test seams are forbidden when managing the real system hosts file."
}
if ($isSystemHosts -and -not (Test-IsAdministrator)) {
    throw "An elevated Windows PowerShell session is required for the system hosts file."
}
if (-not (Test-Path -LiteralPath $HostsPath -PathType Leaf)) {
    throw "HostsPath does not exist; refusing to create a replacement without an ACL baseline."
}

$backupPath = $HostsPath + ".platform-ai.bak"
$mutex = New-Object Threading.Mutex($false, $MutexName)
$lockTaken = $false
try {
    try {
        $lockTaken = $mutex.WaitOne([TimeSpan]::FromSeconds($MutexTimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
        $lockTaken = $true
    }
    if (-not $lockTaken) {
        throw "Timed out waiting for the private gateway hosts-file mutex."
    }

    $originalAcl = Get-Acl -LiteralPath $HostsPath
    $currentContent = Read-Utf8WithoutBom -Path $HostsPath

    if ($RestoreBackup) {
        if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            throw "No platform-ai hosts backup exists to restore."
        }
        $restoredContent = Read-Utf8WithoutBom -Path $backupPath
        if ($PSCmdlet.ShouldProcess($HostsPath, "restore the platform-ai hosts backup")) {
            Write-HostsFileAtomic -Path $HostsPath -Content $restoredContent `
                -OriginalAcl $originalAcl
            Invoke-DnsFlush -Action $DnsFlushAction
        }
        return
    }

    $baseContent = Remove-ManagedGatewayContent -Content $currentContent `
        -Hostname $GatewayHostname -IPv4 $GatewayIPv4
    $desiredContent = if ($Remove) {
        $baseContent
    } else {
        New-ManagedGatewayContent -BaseContent $baseContent `
            -Hostname $GatewayHostname -IPv4 $GatewayIPv4
    }

    $operation = if ($Remove) { "remove the private gateway test shim" } `
        else { "apply the private gateway test shim" }
    $mutated = $false
    if ($desiredContent -ne $currentContent) {
        if ($PSCmdlet.ShouldProcess($HostsPath, $operation)) {
            Write-HostsFileAtomic -Path $HostsPath -Content $desiredContent `
                -OriginalAcl $originalAcl -BackupPath $backupPath
            $mutated = $true
        } else {
            return
        }
    }

    if ($Remove) {
        if ($mutated) { Invoke-DnsFlush -Action $DnsFlushAction }
        return
    }

    try {
        Invoke-DnsFlush -Action $DnsFlushAction
        Assert-GatewayResolution -Hostname $GatewayHostname -IPv4 $GatewayIPv4 `
            -Probe $ResolverProbe
    } catch {
        $verificationError = $_
        $rollbackError = $null
        if ($mutated -and (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            try {
                $rollbackContent = Read-Utf8WithoutBom -Path $backupPath
                Write-HostsFileAtomic -Path $HostsPath -Content $rollbackContent `
                    -OriginalAcl $originalAcl
                try { Invoke-DnsFlush -Action $DnsFlushAction } catch { }
            } catch {
                $rollbackError = $_
            }
        }
        if ($null -ne $rollbackError) {
            throw ("Gateway resolution verification failed and hosts rollback also failed. " +
                "Verification: {0}; rollback: {1}" -f
                $verificationError.Exception.Message,
                $rollbackError.Exception.Message)
        }
        throw $verificationError.Exception
    }
} finally {
    if ($lockTaken) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
