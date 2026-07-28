# Strict, non-executable Windows runtime configuration for live-stt-service.
# Secret values are DPAPI LocalMachine blobs. Values are never written to output.

Set-StrictMode -Version 2.0

try {
    Add-Type -AssemblyName System.Security -ErrorAction Stop
} catch {
    throw "Windows DPAPI support assembly could not be loaded."
}

$script:LiveSttSystemSid = "S-1-5-18"
$script:LiveSttAdministratorsSid = "S-1-5-32-544"
$script:LiveSttDpapiEntropy = [Text.Encoding]::UTF8.GetBytes(
    "platform-ai/live-stt/runtime-secret/v1"
)

function Get-LiveSttRuntimeConfigPath {
    if ($env:OS -ne "Windows_NT" -or [string]::IsNullOrWhiteSpace($env:ProgramData)) {
        throw "The live-stt runtime config is available only on Windows."
    }
    return Join-Path $env:ProgramData "Acik\platform-ai\live-stt.env"
}

function Get-LiveSttRuntimeConfigSchema {
    return @{
        "STT_REQUEST_TIMEOUT" = @{ Target = "STT_REQUEST_TIMEOUT"; Kind = "integer"; Min = 1; Max = 600 }
        "STT_FINAL_BEAM_SIZE" = @{ Target = "STT_FINAL_BEAM_SIZE"; Kind = "integer"; Min = 1; Max = 10 }
        "STT_SILENCE_RMS" = @{ Target = "STT_SILENCE_RMS"; Kind = "decimal"; Min = 0.0001; Max = 0.05 }
        "STT_MIN_SPEECH_RMS" = @{ Target = "STT_MIN_SPEECH_RMS"; Kind = "decimal"; Min = 0.0001; Max = 0.05 }
        "STT_CHUNK_CONSUMER_ENABLED" = @{ Target = "STT_CHUNK_CONSUMER_ENABLED"; Kind = "boolean" }
        "STT_REDIS_URL_DPAPI" = @{ Target = "STT_REDIS_URL"; Kind = "redis-secret" }
        "STT_CHUNK_STREAM_PREFIX" = @{ Target = "STT_CHUNK_STREAM_PREFIX"; Kind = "name" }
        "STT_CHUNK_PARTITION_COUNT" = @{ Target = "STT_CHUNK_PARTITION_COUNT"; Kind = "integer"; Min = 1; Max = 100 }
        "STT_CHUNK_CONSUMER_GROUP" = @{ Target = "STT_CHUNK_CONSUMER_GROUP"; Kind = "name" }
        "STT_CHUNK_CONSUMER_NAME" = @{ Target = "STT_CHUNK_CONSUMER_NAME"; Kind = "name" }
        "STT_CHUNK_BLOCK_MS" = @{ Target = "STT_CHUNK_BLOCK_MS"; Kind = "integer"; Min = 100; Max = 60000 }
        "STT_CHUNK_BATCH_SIZE" = @{ Target = "STT_CHUNK_BATCH_SIZE"; Kind = "integer"; Min = 1; Max = 1000 }
        "STT_CHUNK_DEDUP_CACHE_SIZE" = @{ Target = "STT_CHUNK_DEDUP_CACHE_SIZE"; Kind = "integer"; Min = 64; Max = 1000000 }
        "STT_CHUNK_CLAIM_IDLE_MS" = @{ Target = "STT_CHUNK_CLAIM_IDLE_MS"; Kind = "integer"; Min = 1000; Max = 3600000 }
        "STT_CHUNK_CLAIM_EVERY_LOOPS" = @{ Target = "STT_CHUNK_CLAIM_EVERY_LOOPS"; Kind = "integer"; Min = 1; Max = 10000 }
        "STT_CHUNK_TRIM_MAXLEN" = @{ Target = "STT_CHUNK_TRIM_MAXLEN"; Kind = "integer"; Min = 100; Max = 1000000 }
    }
}

function ConvertTo-LiveSttSidValue {
    param([Parameter(Mandatory = $true)]$IdentityReference)

    try {
        return $IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Live STT runtime config ACL contains an untranslatable identity."
    }
}

function Resolve-LiveSttFixedLocalPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    if ($env:OS -ne "Windows_NT") { throw "$Purpose is supported only on Windows." }
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.StartsWith("\\") -or
        $Path.StartsWith("\\?\") -or $Path.StartsWith("\\.\") -or
        -not [IO.Path]::IsPathRooted($Path) -or $Path -match '^[A-Za-z]:[^\\/]') {
        throw "$Purpose must use an absolute local drive path."
    }
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.Length -gt 240 -or ($full.Length -gt 2 -and $full.Substring(2).Contains(":"))) {
        throw "$Purpose path is unsupported."
    }
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($full))
    if ($drive.DriveType -ne [IO.DriveType]::Fixed) {
        throw "$Purpose must reside on a fixed local volume."
    }
    $cursor = $full
    while ($cursor -and (Test-Path -LiteralPath $cursor)) {
        $item = Get-Item -LiteralPath $cursor -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Purpose must not traverse a reparse point."
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $full
}

function Get-LiveSttRuntimeRoot {
    return [IO.Path]::GetFullPath(
        (Join-Path $env:ProgramData "Acik\platform-ai")
    ).TrimEnd("\")
}

function Assert-LiveSttRuntimeConfigPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Resolve-LiveSttFixedLocalPath -Path $Path -Purpose "Live STT runtime config"
    $root = Get-LiveSttRuntimeRoot
    if (-not $full.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Live STT runtime config must reside under its hardened ProgramData root."
    }
    return $full
}

function Assert-LiveSttRuntimeConfigAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Live STT runtime config ACL inheritance must be disabled."
    }
    $allowed = @($script:LiveSttSystemSid, $script:LiveSttAdministratorsSid)
    $owner = New-Object Security.Principal.NTAccount($acl.Owner)
    $ownerSid = ConvertTo-LiveSttSidValue -IdentityReference $owner
    if ($allowed -notcontains $ownerSid) {
        throw "Live STT runtime config owner must be SYSTEM or BUILTIN Administrators."
    }
    $seen = @{}
    foreach ($rule in @($acl.Access)) {
        $sid = ConvertTo-LiveSttSidValue -IdentityReference $rule.IdentityReference
        if ($rule.IsInherited -or $allowed -notcontains $sid -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw "Live STT runtime config ACL contains an unexpected rule."
        }
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
            throw "Live STT runtime config ACL principals require FullControl."
        }
        if ($Directory -and
            (($rule.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ContainerInherit) -eq 0 -or
             ($rule.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ObjectInherit) -eq 0)) {
            throw "Live STT runtime config directory ACL must protect child objects."
        }
        $seen[$sid] = $true
    }
    foreach ($sid in $allowed) {
        if (-not $seen.ContainsKey($sid)) {
            throw "Live STT runtime config ACL misses a required principal."
        }
    }
}

function Clear-LiveSttManagedProcessEnvironment {
    foreach ($entry in (Get-LiveSttRuntimeConfigSchema).Values) {
        [Environment]::SetEnvironmentVariable(
            [string]$entry.Target,
            $null,
            [EnvironmentVariableTarget]::Process
        )
    }
}

function ConvertFrom-LiveSttRuntimeValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)]$Spec
    )

    switch ([string]$Spec.Kind) {
        "boolean" {
            if ($Value -notmatch '^(?i:true|false)$') { throw "$Key must be true or false." }
            return $Value.ToLowerInvariant()
        }
        "integer" {
            $number = 0
            if (-not [int]::TryParse($Value, [ref]$number) -or
                $number -lt [int]$Spec.Min -or $number -gt [int]$Spec.Max) {
                throw "$Key is outside its allowed integer range."
            }
            return "$number"
        }
        "decimal" {
            if ($Value -notmatch '^0\.[0-9]{1,6}$') {
                throw "$Key must use canonical invariant decimal notation."
            }
            $number = [decimal]0
            $style = [Globalization.NumberStyles]::AllowDecimalPoint
            $culture = [Globalization.CultureInfo]::InvariantCulture
            if (-not [decimal]::TryParse($Value, $style, $culture, [ref]$number) -or
                $number -lt [decimal]$Spec.Min -or $number -gt [decimal]$Spec.Max) {
                throw "$Key is outside its allowed decimal range."
            }
            $canonical = $number.ToString("0.######", $culture)
            if ($canonical -cne $Value) {
                throw "$Key must use canonical invariant decimal notation."
            }
            return $canonical
        }
        "name" {
            if ($Value -notmatch '^[A-Za-z0-9._:-]{1,128}$') {
                throw "$Key contains unsupported characters."
            }
            return $Value
        }
        "redis-secret" {
            try {
                $ciphertext = [Convert]::FromBase64String($Value)
                $plaintext = [Security.Cryptography.ProtectedData]::Unprotect(
                    $ciphertext,
                    $script:LiveSttDpapiEntropy,
                    [Security.Cryptography.DataProtectionScope]::LocalMachine
                )
                $decoded = [Text.Encoding]::UTF8.GetString($plaintext)
                [Array]::Clear($plaintext, 0, $plaintext.Length)
                $uri = $null
                if (-not [Uri]::TryCreate($decoded, [UriKind]::Absolute, [ref]$uri) -or
                    @("redis", "rediss") -notcontains $uri.Scheme -or
                    [string]::IsNullOrWhiteSpace($uri.Host)) {
                    throw "invalid"
                }
                return $decoded
            } catch {
                throw "$Key is not a valid DPAPI LocalMachine Redis URL."
            }
        }
        default { throw "$Key has an unsupported schema kind." }
    }
}

function Assert-LiveSttEffectiveRmsPair {
    param(
        [Parameter(Mandatory = $true)]$Values,
        $FallbackValues = $null
    )

    $hasSilenceOverride = $Values.ContainsKey("STT_SILENCE_RMS")
    $hasMinimumOverride = $Values.ContainsKey("STT_MIN_SPEECH_RMS")
    if ($hasSilenceOverride -xor $hasMinimumOverride) {
        throw "An RMS override must provide STT_SILENCE_RMS and STT_MIN_SPEECH_RMS together."
    }

    $silence = if ($hasSilenceOverride) {
        [string]$Values["STT_SILENCE_RMS"]
    } elseif ($null -ne $FallbackValues -and $FallbackValues.ContainsKey("STT_SILENCE_RMS")) {
        [string]$FallbackValues["STT_SILENCE_RMS"]
    } else { [string]$env:STT_SILENCE_RMS }
    $minimum = if ($hasMinimumOverride) {
        [string]$Values["STT_MIN_SPEECH_RMS"]
    } elseif ($null -ne $FallbackValues -and
        $FallbackValues.ContainsKey("STT_MIN_SPEECH_RMS")) {
        [string]$FallbackValues["STT_MIN_SPEECH_RMS"]
    } else { [string]$env:STT_MIN_SPEECH_RMS }

    if ([string]::IsNullOrWhiteSpace($silence) -and
        [string]::IsNullOrWhiteSpace($minimum)) {
        return
    }
    if ([string]::IsNullOrWhiteSpace($silence) -or
        [string]::IsNullOrWhiteSpace($minimum)) {
        throw "An RMS override requires a complete source or runtime RMS pair."
    }

    $schema = Get-LiveSttRuntimeConfigSchema
    $silence = ConvertFrom-LiveSttRuntimeValue -Key "STT_SILENCE_RMS" `
        -Value $silence -Spec $schema["STT_SILENCE_RMS"]
    $minimum = ConvertFrom-LiveSttRuntimeValue -Key "STT_MIN_SPEECH_RMS" `
        -Value $minimum -Spec $schema["STT_MIN_SPEECH_RMS"]
    if ([decimal]$minimum -lt [decimal]$silence) {
        throw "STT_MIN_SPEECH_RMS must be greater than or equal to STT_SILENCE_RMS."
    }
}

function Import-LiveSttRuntimeEnvironment {
    param(
        [string]$ConfigPath = "",
        [switch]$SkipAclValidation
    )

    if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
        $ConfigPath = Get-LiveSttRuntimeConfigPath
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return $false }
    if ($SkipAclValidation -and $env:CI -ne "true") {
        throw "Live STT runtime config ACL validation can be skipped only in CI."
    }
    if (-not $SkipAclValidation) {
        $ConfigPath = Assert-LiveSttRuntimeConfigPath -Path $ConfigPath
        $vendorRoot = Join-Path $env:ProgramData "Acik"
        $runtimeRoot = Get-LiveSttRuntimeRoot
        Assert-LiveSttRuntimeConfigAcl -Path $vendorRoot -Directory
        Assert-LiveSttRuntimeConfigAcl -Path $runtimeRoot -Directory
        Assert-LiveSttRuntimeConfigAcl -Path $ConfigPath
    }

    $bytes = [IO.File]::ReadAllBytes($ConfigPath)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and
        $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "Live STT runtime config must be UTF-8 without BOM."
    }
    try {
        $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes)
    } catch {
        throw "Live STT runtime config is not valid UTF-8."
    }

    $schema = Get-LiveSttRuntimeConfigSchema
    $values = @{}
    foreach ($rawLine in @($text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) { continue }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) { throw "Live STT runtime config contains an invalid line." }
        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1)
        if (-not $schema.ContainsKey($key)) { throw "Live STT runtime config uses an unknown key." }
        if ($values.ContainsKey($key)) { throw "Live STT runtime config contains a duplicate key." }
        if ($value -match '[\x00-\x1f]') { throw "$key contains a control character." }
        $values[$key] = ConvertFrom-LiveSttRuntimeValue -Key $key -Value $value -Spec $schema[$key]
    }

    # Validate the effective pair before mutating any process environment value.
    Assert-LiveSttEffectiveRmsPair -Values $values

    if ($values.ContainsKey("STT_SILENCE_RMS") -and
        $values.ContainsKey("STT_MIN_SPEECH_RMS")) {
        [Environment]::SetEnvironmentVariable(
            "STT_SPEECH_GATE_RMS_SOURCE",
            "host-override",
            [EnvironmentVariableTarget]::Process
        )
    }

    foreach ($key in $values.Keys) {
        [Environment]::SetEnvironmentVariable(
            [string]$schema[$key].Target,
            [string]$values[$key],
            [EnvironmentVariableTarget]::Process
        )
    }
    return $true
}
