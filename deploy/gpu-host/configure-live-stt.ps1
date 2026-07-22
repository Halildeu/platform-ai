# Provision or rotate the live-STT host-local runtime configuration.
# Run from elevated Windows PowerShell 5.1. Redis credentials are accepted only
# as SecureString values and stored as DPAPI LocalMachine ciphertext.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Security.SecureString]$RedisUrl,
    [ValidateSet("", "true", "false")][string]$ChunkConsumerEnabled = "",
    [ValidateRange(0, 300)][int]$RequestTimeout = 0,
    [string]$SilenceRms = "",
    [string]$MinSpeechRms = "",
    [string]$ChunkStreamPrefix = "",
    [ValidateRange(0, 100)][int]$ChunkPartitionCount = 0,
    [string]$ChunkConsumerGroup = "",
    [string]$ChunkConsumerName = "",
    [ValidateRange(0, 60000)][int]$ChunkBlockMs = 0,
    [ValidateRange(0, 1000)][int]$ChunkBatchSize = 0,
    [ValidateRange(0, 1000000)][int]$ChunkDedupCacheSize = 0,
    [ValidateRange(0, 3600000)][int]$ChunkClaimIdleMs = 0,
    [ValidateRange(0, 10000)][int]$ChunkClaimEveryLoops = 0,
    [ValidateRange(0, 1000000)][int]$ChunkTrimMaxlen = 0,
    [switch]$RemoveLegacyAfterVerifiedMigration
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

if ($env:OS -ne "Windows_NT") {
    throw "Live STT provisioning is supported only on Windows."
}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated Administrator PowerShell."
}

$scriptDir = Split-Path $PSCommandPath -Parent
. (Join-Path $scriptDir "live-stt-runtime-env.ps1")
. (Join-Path $scriptDir "live-stt-runtime-contract.ps1")

$RepoRoot = Resolve-LiveSttFixedLocalPath -Path $RepoRoot `
    -Purpose "Platform-ai deploy repository"
$configPath = Get-LiveSttRuntimeConfigPath
$legacyConfigPath = Join-Path $RepoRoot "deploy\gpu-host\env.local.ps1"
$configPath = Assert-LiveSttRuntimeConfigPath -Path $configPath
if (-not $PSCmdlet.ShouldProcess(
        $configPath,
        "write and verify DPAPI-protected live-STT runtime configuration"
    )) {
    return
}

function New-LiveSttProvisionAcl {
    param([switch]$Directory)

    $system = New-Object Security.Principal.SecurityIdentifier(
        $script:LiveSttSystemSid
    )
    $administrators = New-Object Security.Principal.SecurityIdentifier(
        $script:LiveSttAdministratorsSid
    )
    $acl = if ($Directory) {
        New-Object Security.AccessControl.DirectorySecurity
    } else {
        New-Object Security.AccessControl.FileSecurity
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) {
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
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

function Initialize-LiveSttProvisionDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Resolve-LiveSttFixedLocalPath -Path $Path `
        -Purpose "Live STT runtime directory"
    if (Test-Path -LiteralPath $full) {
        if (-not (Test-Path -LiteralPath $full -PathType Container)) {
            throw "Live STT runtime directory path is not a directory."
        }
    } else {
        New-Item -ItemType Directory -Path $full -Force | Out-Null
    }
    Set-Acl -LiteralPath $full -AclObject (New-LiveSttProvisionAcl -Directory)
    Assert-LiveSttRuntimeConfigAcl -Path $full -Directory
    return $full
}

function Initialize-LiveSttProvisionRoot {
    $vendorRoot = Join-Path $env:ProgramData "Acik"
    [void](Initialize-LiveSttProvisionDirectory -Path $vendorRoot)
    $runtimeRoot = Get-LiveSttRuntimeRoot
    [void](Initialize-LiveSttProvisionDirectory -Path $runtimeRoot)
    return $runtimeRoot
}

function Get-LiveSttRedisHashFromBlob {
    param([Parameter(Mandatory = $true)][string]$ProtectedBase64)

    $protected = $null
    $plainBytes = $null
    $plain = $null
    try {
        $protected = [Convert]::FromBase64String($ProtectedBase64)
    } catch {
        throw "STT_REDIS_URL_DPAPI is not valid base64."
    }
    try {
        $plainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
            $protected,
            $script:LiveSttDpapiEntropy,
            [Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        try {
            $plain = (New-Object Text.UTF8Encoding($false, $true)).GetString($plainBytes)
            $uri = $null
            if (-not [Uri]::TryCreate($plain, [UriKind]::Absolute, [ref]$uri) -or
                @("redis", "rediss") -notcontains $uri.Scheme -or
                [string]::IsNullOrWhiteSpace($uri.Host)) {
                throw "STT_REDIS_URL_DPAPI does not contain a valid Redis URL."
            }
            $sha256 = [Security.Cryptography.SHA256]::Create()
            try {
                return [Convert]::ToBase64String($sha256.ComputeHash($plainBytes))
            } finally {
                $sha256.Dispose()
            }
        } finally {
            $plain = $null
            [Array]::Clear($plainBytes, 0, $plainBytes.Length)
        }
    } catch {
        throw "STT_REDIS_URL_DPAPI cannot be decrypted and validated on this machine."
    } finally {
        if ($null -ne $protected) {
            [Array]::Clear($protected, 0, $protected.Length)
        }
    }
}

function Protect-LiveSttRedisSecureString {
    param([Parameter(Mandatory = $true)][Security.SecureString]$Value)

    $bstr = [IntPtr]::Zero
    $plain = $null
    $plainBytes = $null
    $protected = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        $uri = $null
        if (-not [Uri]::TryCreate($plain, [UriKind]::Absolute, [ref]$uri) -or
            @("redis", "rediss") -notcontains $uri.Scheme -or
            [string]::IsNullOrWhiteSpace($uri.Host)) {
            throw "RedisUrl must be an absolute redis:// or rediss:// URL."
        }
        $plainBytes = [Text.Encoding]::UTF8.GetBytes($plain)
        $protected = [Security.Cryptography.ProtectedData]::Protect(
            $plainBytes,
            $script:LiveSttDpapiEntropy,
            [Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            return [pscustomobject]@{
                Blob = [Convert]::ToBase64String($protected)
                Hash = [Convert]::ToBase64String($sha256.ComputeHash($plainBytes))
            }
        } finally {
            $sha256.Dispose()
        }
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        $plain = $null
        if ($null -ne $plainBytes) {
            [Array]::Clear($plainBytes, 0, $plainBytes.Length)
        }
        if ($null -ne $protected) {
            [Array]::Clear($protected, 0, $protected.Length)
        }
    }
}

function Read-LiveSttProvisionConfig {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Assert-LiveSttRuntimeConfigPath -Path $Path
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "Live STT runtime config file does not exist."
    }
    Assert-LiveSttRuntimeConfigAcl -Path (Join-Path $env:ProgramData "Acik") -Directory
    Assert-LiveSttRuntimeConfigAcl -Path (Get-LiveSttRuntimeRoot) -Directory
    Assert-LiveSttRuntimeConfigAcl -Path $full
    $bytes = [IO.File]::ReadAllBytes($full)
    try {
        if ($bytes.Length -gt 65536) {
            throw "Live STT runtime config exceeds the 64 KiB limit."
        }
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and
            $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            throw "Live STT runtime config must be UTF-8 without BOM."
        }
        $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes)
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    $schema = Get-LiveSttRuntimeConfigSchema
    $values = New-Object 'Collections.Generic.Dictionary[string,string]'(
        [StringComparer]::Ordinal
    )
    foreach ($rawLine in @($text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) { continue }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            throw "Live STT runtime config contains an invalid line."
        }
        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1)
        if (-not $schema.ContainsKey($key) -or -not ($schema.Keys -ccontains $key)) {
            throw "Live STT runtime config uses an unknown key."
        }
        if ($values.ContainsKey($key)) {
            throw "Live STT runtime config contains a duplicate key."
        }
        if ($value -match '[\x00-\x1f]') {
            throw "$key contains a control character."
        }
        $validated = ConvertFrom-LiveSttRuntimeValue `
            -Key $key -Value $value -Spec $schema[$key]
        $validated = $null
        $values.Add($key, $value)
    }
    return $values
}

function Assert-LiveSttProvisionCandidate {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)]$Expected,
        [string]$ExpectedRedisHash = ""
    )

    if ($Actual.Count -ne $Expected.Count) {
        throw "Live STT runtime config readback key count differs from the candidate."
    }
    foreach ($key in $Expected.Keys) {
        if (-not $Actual.ContainsKey($key) -or $Actual[$key] -ne $Expected[$key]) {
            throw "Live STT runtime config readback differs at $key."
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedRedisHash)) {
        if (-not $Actual.ContainsKey("STT_REDIS_URL_DPAPI")) {
            throw "Live STT runtime config readback misses its Redis credential."
        }
        $actualHash = Get-LiveSttRedisHashFromBlob `
            -ProtectedBase64 $Actual["STT_REDIS_URL_DPAPI"]
        try {
            if ($actualHash -cne $ExpectedRedisHash) {
                throw "Live STT runtime Redis credential readback differs from the supplied value."
            }
        } finally {
            $actualHash = $null
        }
    }
}

function ConvertTo-LiveSttProvisionContent {
    param([Parameter(Mandatory = $true)]$Values)

    $order = @(
        "STT_REQUEST_TIMEOUT",
        "STT_SILENCE_RMS",
        "STT_MIN_SPEECH_RMS",
        "STT_CHUNK_CONSUMER_ENABLED",
        "STT_REDIS_URL_DPAPI",
        "STT_CHUNK_STREAM_PREFIX",
        "STT_CHUNK_PARTITION_COUNT",
        "STT_CHUNK_CONSUMER_GROUP",
        "STT_CHUNK_CONSUMER_NAME",
        "STT_CHUNK_BLOCK_MS",
        "STT_CHUNK_BATCH_SIZE",
        "STT_CHUNK_DEDUP_CACHE_SIZE",
        "STT_CHUNK_CLAIM_IDLE_MS",
        "STT_CHUNK_CLAIM_EVERY_LOOPS",
        "STT_CHUNK_TRIM_MAXLEN"
    )
    $lines = @(
        "# platform-ai live-STT runtime config v1",
        "# Secret fields are DPAPI LocalMachine ciphertext; never copy between hosts."
    )
    foreach ($key in $order) {
        if ($Values.ContainsKey($key)) {
            $lines += "$key=$($Values[$key])"
        }
    }
    return ($lines -join "`r`n") + "`r`n"
}

function Write-LiveSttProvisionConfigAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)]$Expected,
        [string]$ExpectedRedisHash = ""
    )

    $full = Assert-LiveSttRuntimeConfigPath -Path $Path
    $directory = Split-Path -Parent $full
    $temp = Join-Path $directory (".live-stt-{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
    $backup = Assert-LiveSttRuntimeConfigPath -Path "$full.bak"
    $failed = Assert-LiveSttRuntimeConfigPath -Path "$full.failed"
    $hadPrevious = Test-Path -LiteralPath $full -PathType Leaf
    try {
        [IO.File]::WriteAllBytes($temp, @())
        Set-Acl -LiteralPath $temp -AclObject (New-LiveSttProvisionAcl)
        [IO.File]::WriteAllText($temp, $Content, (New-Object Text.UTF8Encoding($false)))
        Assert-LiveSttRuntimeConfigAcl -Path $temp
        $candidate = Read-LiveSttProvisionConfig -Path $temp
        Assert-LiveSttProvisionCandidate -Actual $candidate -Expected $Expected `
            -ExpectedRedisHash $ExpectedRedisHash

        if (Test-Path -LiteralPath $backup) {
            Remove-Item -LiteralPath $backup -Force
        }
        if ($hadPrevious) {
            [IO.File]::Replace($temp, $full, $backup, $true)
            Set-Acl -LiteralPath $backup -AclObject (New-LiveSttProvisionAcl)
            Assert-LiveSttRuntimeConfigAcl -Path $backup
        } else {
            [IO.File]::Move($temp, $full)
        }
        Set-Acl -LiteralPath $full -AclObject (New-LiveSttProvisionAcl)
        try {
            $readback = Read-LiveSttProvisionConfig -Path $full
            Assert-LiveSttProvisionCandidate -Actual $readback -Expected $Expected `
                -ExpectedRedisHash $ExpectedRedisHash
        } catch {
            if ($hadPrevious -and (Test-Path -LiteralPath $backup -PathType Leaf)) {
                if (Test-Path -LiteralPath $failed) {
                    Remove-Item -LiteralPath $failed -Force
                }
                [IO.File]::Replace($backup, $full, $failed, $true)
                Set-Acl -LiteralPath $full -AclObject (New-LiveSttProvisionAcl)
            } elseif (Test-Path -LiteralPath $full -PathType Leaf) {
                Remove-Item -LiteralPath $full -Force
            }
            throw
        }
        if (Test-Path -LiteralPath $backup) {
            Remove-Item -LiteralPath $backup -Force
        }
        if (Test-Path -LiteralPath $failed) {
            Remove-Item -LiteralPath $failed -Force
        }
    } finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force
        }
    }
}

function Set-LiveSttPublicValue {
    param(
        [Parameter(Mandatory = $true)]$Values,
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$Supplied = "",
        [string]$InitialDefault = ""
    )

    if (-not [string]::IsNullOrWhiteSpace($Supplied)) {
        $Values[$Key] = $Supplied
    } elseif (-not $Values.ContainsKey($Key) -and
        -not [string]::IsNullOrWhiteSpace($InitialDefault)) {
        $Values[$Key] = $InitialDefault
    }
}

$mutex = New-Object Threading.Mutex($false, "Global\platform-ai-live-stt-config-v1")
$lockTaken = $false
$redisMaterial = $null
$expectedRedisHash = ""
try {
    $lockTaken = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
    if (-not $lockTaken) {
        throw "Timed out waiting for the live STT runtime config lock."
    }
    [void](Initialize-LiveSttProvisionRoot)
    if (Test-Path -LiteralPath $configPath) {
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            throw "The fixed live STT runtime config path is not a file."
        }
        Set-Acl -LiteralPath $configPath -AclObject (New-LiveSttProvisionAcl)
        $existing = Read-LiveSttProvisionConfig -Path $configPath
    } else {
        $existing = New-Object 'Collections.Generic.Dictionary[string,string]'(
            [StringComparer]::Ordinal
        )
    }

    $values = New-Object 'Collections.Generic.Dictionary[string,string]'(
        [StringComparer]::Ordinal
    )
    foreach ($key in $existing.Keys) { $values.Add($key, $existing[$key]) }
    $hasSilenceRms = -not [string]::IsNullOrWhiteSpace($SilenceRms)
    $hasMinSpeechRms = -not [string]::IsNullOrWhiteSpace($MinSpeechRms)
    if ($hasSilenceRms -xor $hasMinSpeechRms) {
        throw "SilenceRms and MinSpeechRms must be supplied together."
    }
    Set-LiveSttPublicValue -Values $values -Key "STT_REQUEST_TIMEOUT" `
        -Supplied $(if ($RequestTimeout -gt 0) { "$RequestTimeout" } else { "" }) `
        -InitialDefault "180"
    Set-LiveSttPublicValue -Values $values -Key "STT_SILENCE_RMS" `
        -Supplied $SilenceRms
    Set-LiveSttPublicValue -Values $values -Key "STT_MIN_SPEECH_RMS" `
        -Supplied $MinSpeechRms
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_CONSUMER_ENABLED" `
        -Supplied $ChunkConsumerEnabled.ToLowerInvariant() -InitialDefault "false"
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_STREAM_PREFIX" `
        -Supplied $ChunkStreamPrefix
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_PARTITION_COUNT" `
        -Supplied $(if ($ChunkPartitionCount -gt 0) { "$ChunkPartitionCount" } else { "" })
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_CONSUMER_GROUP" `
        -Supplied $ChunkConsumerGroup
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_CONSUMER_NAME" `
        -Supplied $ChunkConsumerName
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_BLOCK_MS" `
        -Supplied $(if ($ChunkBlockMs -gt 0) { "$ChunkBlockMs" } else { "" })
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_BATCH_SIZE" `
        -Supplied $(if ($ChunkBatchSize -gt 0) { "$ChunkBatchSize" } else { "" })
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_DEDUP_CACHE_SIZE" `
        -Supplied $(if ($ChunkDedupCacheSize -gt 0) { "$ChunkDedupCacheSize" } else { "" })
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_CLAIM_IDLE_MS" `
        -Supplied $(if ($ChunkClaimIdleMs -gt 0) { "$ChunkClaimIdleMs" } else { "" })
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_CLAIM_EVERY_LOOPS" `
        -Supplied $(if ($ChunkClaimEveryLoops -gt 0) { "$ChunkClaimEveryLoops" } else { "" })
    Set-LiveSttPublicValue -Values $values -Key "STT_CHUNK_TRIM_MAXLEN" `
        -Supplied $(if ($ChunkTrimMaxlen -gt 0) { "$ChunkTrimMaxlen" } else { "" })

    if ($null -eq $RedisUrl -and -not $values.ContainsKey("STT_REDIS_URL_DPAPI")) {
        $RedisUrl = Read-Host "Redis URL (stored with DPAPI LocalMachine)" -AsSecureString
    }
    if ($null -ne $RedisUrl) {
        $redisMaterial = Protect-LiveSttRedisSecureString -Value $RedisUrl
        $values["STT_REDIS_URL_DPAPI"] = $redisMaterial.Blob
        $expectedRedisHash = $redisMaterial.Hash
    } elseif ($values.ContainsKey("STT_REDIS_URL_DPAPI")) {
        $expectedRedisHash = Get-LiveSttRedisHashFromBlob `
            -ProtectedBase64 $values["STT_REDIS_URL_DPAPI"]
    }
    if ($values["STT_CHUNK_CONSUMER_ENABLED"] -eq "true" -and
        -not $values.ContainsKey("STT_REDIS_URL_DPAPI")) {
        throw "An enabled chunk consumer requires a DPAPI-protected Redis URL."
    }

    $schema = Get-LiveSttRuntimeConfigSchema
    foreach ($key in $values.Keys) {
        $validated = ConvertFrom-LiveSttRuntimeValue `
            -Key $key -Value $values[$key] -Spec $schema[$key]
        $validated = $null
    }
    $sourceRms = @{
        "STT_SILENCE_RMS" = "$script:LiveSttSilenceRms"
        "STT_MIN_SPEECH_RMS" = "$script:LiveSttMinSpeechRms"
    }
    Assert-LiveSttEffectiveRmsPair -Values $values -FallbackValues $sourceRms
    $content = ConvertTo-LiveSttProvisionContent -Values $values
    Write-LiveSttProvisionConfigAtomic -Path $configPath -Content $content `
        -Expected $values -ExpectedRedisHash $expectedRedisHash

    if (Test-Path -LiteralPath $legacyConfigPath) {
        if (-not $RemoveLegacyAfterVerifiedMigration) {
            throw "DPAPI runtime config was verified, but legacy env.local.ps1 remains. The service stays fail-closed. Rerun with -RemoveLegacyAfterVerifiedMigration only after the replacement secret and host erasure action are approved."
        }
        if (-not $PSCmdlet.ShouldProcess(
                $legacyConfigPath,
                "remove legacy plaintext config after verified DPAPI migration"
            )) {
            throw "Legacy plaintext config removal was declined; the service stays fail-closed."
        }
        Remove-Item -LiteralPath $legacyConfigPath -Force
        if (Test-Path -LiteralPath $legacyConfigPath) {
            throw "Legacy plaintext config removal postcondition failed."
        }
    }
    Write-Host "Live STT runtime config was provisioned and readback verified."
} finally {
    $RedisUrl = $null
    $redisMaterial = $null
    $expectedRedisHash = $null
    if ($lockTaken) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
