# Strict Windows runtime configuration for meeting-ai-service.
#
# This trusted repo script reads a non-executable KEY=VALUE file. Secret values
# are DPAPI LocalMachine blobs at rest and are exposed only to the child process.
# Windows PowerShell 5.1 compatible; never write values to output or errors.

try {
    Add-Type -AssemblyName System.Security -ErrorAction Stop
} catch {
    throw "Windows DPAPI support assembly could not be loaded."
}
if ($null -eq ("MeetingAi.NativeMethods" -as [type])) {
    Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;

namespace MeetingAi {
    public static class NativeMethods {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool MoveFileEx(
            string existingFileName,
            string newFileName,
            int flags
        );
    }
}
"@ -ErrorAction Stop
}

$script:MeetingAiSystemSid = "S-1-5-18"
$script:MeetingAiAdministratorsSid = "S-1-5-32-544"
$script:MeetingAiDpapiEntropy = [Text.Encoding]::UTF8.GetBytes(
    "platform-ai/meeting-ai/runtime-secret/v1"
)

function Get-MeetingAiConfigSchema {
    return @{
        "MAI_APP_ENV" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_ENABLED" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_BASE_URL" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_TOKEN_URL" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_CLIENT_ID" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI" = @{
            Required = $true
            SecretTarget = "MAI_MEETING_SERVICE_CLIENT_SECRET"
        }
        "MAI_MEETING_SERVICE_AUDIENCE" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_SCOPE" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_TLS_MODE" = @{ Required = $true; SecretTarget = "" }
        "MAI_MEETING_SERVICE_TLS_CA_PATH" = @{ Required = $false; SecretTarget = "" }
        "MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH" = @{
            Required = $false
            SecretTarget = ""
        }
        "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI" = @{
            Required = $false
            SecretTarget = "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_PATH"
        }
        "MAI_MEETING_SERVICE_TLS_RELOAD_INTERVAL_SEC" = @{
            Required = $false
            SecretTarget = ""
        }
        "MAI_INGESTION_STORE_PATH" = @{ Required = $true; SecretTarget = "" }
        "MAI_INGESTION_ACTIVE_KEY_ID" = @{ Required = $true; SecretTarget = "" }
        "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI" = @{
            Required = $true
            SecretTarget = "MAI_INGESTION_ENCRYPTION_KEYS_JSON"
        }
        "MAI_INGESTION_TIMEOUT_SEC" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_MAX_ATTEMPTS" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_BASE_BACKOFF_SEC" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_MAX_BACKOFF_SEC" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_POLL_INTERVAL_SEC" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_LEASE_SEC" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_MAX_ROWS" = @{ Required = $false; SecretTarget = "" }
        "MAI_INGESTION_STALE_AFTER_SEC" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_CONSUMER_ENABLED" = @{ Required = $false; SecretTarget = "" }
        "MAI_ANALYSIS_SPEC_VERSION" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_REDIS_URL_DPAPI" = @{
            Required = $false
            SecretTarget = "MAI_READY_REDIS_URL"
        }
        "MAI_READY_REDIS_STREAM" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_REDIS_GROUP" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_PRODUCER_REPLAY_HORIZON_SEC" = @{
            Required = $false
            SecretTarget = ""
        }
        "MAI_TRANSCRIPT_SERVICE_BASE_URL" = @{ Required = $false; SecretTarget = "" }
        "MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE" = @{
            Required = $false
            SecretTarget = ""
        }
        "MAI_TRANSCRIPT_SERVICE_CAPABILITY_PATH_TEMPLATE" = @{
            Required = $false
            SecretTarget = ""
        }
        "MAI_TRANSCRIPT_SERVICE_TOKEN_URL" = @{ Required = $false; SecretTarget = "" }
        "MAI_TRANSCRIPT_SERVICE_CLIENT_ID" = @{ Required = $false; SecretTarget = "" }
        "MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI" = @{
            Required = $false
            SecretTarget = "MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET"
        }
        "MAI_TRANSCRIPT_SERVICE_AUDIENCE" = @{ Required = $false; SecretTarget = "" }
        "MAI_TRANSCRIPT_SERVICE_SCOPE" = @{ Required = $false; SecretTarget = "" }
        "MAI_TRANSCRIPT_SERVICE_CAPABILITY_SCOPE" = @{
            Required = $false
            SecretTarget = ""
        }
        "MAI_READY_PRE_ENABLE_PERMIT_PATH" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_EXPECTED_GITOPS_COMMIT" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_EXPECTED_POLICY_SHA256" = @{ Required = $false; SecretTarget = "" }
        "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST" = @{
            Required = $false
            SecretTarget = ""
        }
    }
}

function ConvertTo-SidValue {
    param([Parameter(Mandatory = $true)]$IdentityReference)

    try {
        return $IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Runtime config ACL contains an identity that cannot be translated to a SID."
    }
}

function Resolve-FixedLocalPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    if ($env:OS -ne "Windows_NT") {
        throw "$Purpose is supported only on Windows."
    }
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Purpose path is empty."
    }
    if ($Path.StartsWith("\\") -or $Path.StartsWith("\\?\") -or $Path.StartsWith("\\.\")) {
        throw "$Purpose must not use a UNC or device path."
    }
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path -match '^[A-Za-z]:[^\\/]') {
        throw "$Purpose must use an absolute drive path."
    }

    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith("\\") -or $full.StartsWith("\\?\") -or
        $full.StartsWith("\\.\")) {
        throw "$Purpose must not resolve to a UNC or device path."
    }
    if ($full.Length -gt 240) {
        throw "$Purpose path is too long for the Windows service runtime."
    }
    if ($full.Length -gt 2 -and $full.Substring(2).Contains(":")) {
        throw "$Purpose must not use an alternate data stream."
    }

    $root = [IO.Path]::GetPathRoot($full)
    $drive = New-Object IO.DriveInfo($root)
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

function Get-MeetingAiRuntimeRoot {
    if ($env:OS -ne "Windows_NT" -or [string]::IsNullOrWhiteSpace($env:ProgramData)) {
        throw "The meeting-ai runtime root is available only on Windows."
    }
    return [IO.Path]::GetFullPath(
        (Join-Path $env:ProgramData "Acik\platform-ai")
    ).TrimEnd("\")
}

function Assert-MeetingAiRuntimePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $full = Resolve-FixedLocalPath -Path $Path -Purpose $Purpose
    $root = Get-MeetingAiRuntimeRoot
    $prefix = $root + "\"
    if (-not $full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -and
        -not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose must reside under the hardened meeting-ai runtime root."
    }
    return $full
}

function New-MeetingAiAcl {
    param([switch]$Directory)

    $system = New-Object Security.Principal.SecurityIdentifier(
        $script:MeetingAiSystemSid
    )
    $administrators = New-Object Security.Principal.SecurityIdentifier(
        $script:MeetingAiAdministratorsSid
    )
    if ($Directory) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        $propagation = [Security.AccessControl.PropagationFlags]::None
        foreach ($sid in @($system, $administrators)) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                $propagation,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$acl.AddAccessRule($rule)
        }
    } else {
        $acl = New-Object Security.AccessControl.FileSecurity
        foreach ($sid in @($system, $administrators)) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$acl.AddAccessRule($rule)
        }
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    return $acl
}

function Assert-MeetingAiAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Runtime config ACL inheritance must be disabled."
    }

    $owner = New-Object Security.Principal.NTAccount($acl.Owner)
    $ownerSid = ConvertTo-SidValue -IdentityReference $owner
    if ($ownerSid -notin @($script:MeetingAiSystemSid, $script:MeetingAiAdministratorsSid)) {
        throw "Runtime config owner must be SYSTEM or BUILTIN Administrators."
    }

    $seen = @{}
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        $sid = ConvertTo-SidValue -IdentityReference $rule.IdentityReference
        if ($sid -notin @($script:MeetingAiSystemSid, $script:MeetingAiAdministratorsSid)) {
            throw "Runtime config ACL grants access to a principal outside the service allowlist."
        }
        $seen[$sid] = $true
        $full = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $full) -ne $full) {
            throw "Runtime config ACL must grant FullControl to each allowed service principal."
        }
        if ($Directory -and
            (($rule.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ContainerInherit) -eq 0 -or
             ($rule.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ObjectInherit) -eq 0)) {
            throw "Runtime config directory ACL must protect child files and directories."
        }
    }
    foreach ($requiredSid in @($script:MeetingAiSystemSid, $script:MeetingAiAdministratorsSid)) {
        if (-not $seen.ContainsKey($requiredSid)) {
            throw "Runtime config ACL is missing a required service principal."
        }
    }
}

function Initialize-MeetingAiDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Resolve-FixedLocalPath -Path $Path -Purpose "Runtime directory"
    if (-not (Test-Path -LiteralPath $full)) {
        New-Item -ItemType Directory -Path $full -Force | Out-Null
    }
    Set-Acl -LiteralPath $full -AclObject (New-MeetingAiAcl -Directory)
    Assert-MeetingAiAcl -Path $full -Directory
    return $full
}

function Initialize-MeetingAiRuntimeRoot {
    $vendorRoot = Join-Path $env:ProgramData "Acik"
    [void](Initialize-MeetingAiDirectory -Path $vendorRoot)
    $runtimeRoot = Get-MeetingAiRuntimeRoot
    [void](Initialize-MeetingAiDirectory -Path $runtimeRoot)
    return $runtimeRoot
}

function Read-MeetingAiConfigFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Assert-MeetingAiRuntimePath -Path $Path -Purpose "Runtime config"
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "Runtime config file does not exist."
    }
    $parent = Split-Path -Parent $full
    Assert-MeetingAiAcl -Path (Join-Path $env:ProgramData "Acik") -Directory
    Assert-MeetingAiAcl -Path (Get-MeetingAiRuntimeRoot) -Directory
    Assert-MeetingAiAcl -Path $parent -Directory
    Assert-MeetingAiAcl -Path $full

    $bytes = [IO.File]::ReadAllBytes($full)
    if ($bytes.Length -gt 65536) {
        throw "Runtime config exceeds the 64 KiB limit."
    }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and
        $bytes[2] -eq 0xBF) {
        throw "Runtime config must be UTF-8 without BOM."
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $text = $utf8.GetString($bytes)
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    if ($text.IndexOf([char]0) -ge 0) {
        throw "Runtime config contains a NUL character."
    }

    $schema = Get-MeetingAiConfigSchema
    $values = New-Object 'Collections.Generic.Dictionary[string,string]'(
        [StringComparer]::OrdinalIgnoreCase
    )
    $lineNumber = 0
    foreach ($rawLine in $text.Split("`n")) {
        $lineNumber++
        if ($rawLine.Length -gt 8192) {
            throw "Runtime config line $lineNumber exceeds the 8 KiB limit."
        }
        $line = $rawLine.TrimEnd("`r")
        if ($line.IndexOf("`r") -ge 0) {
            throw "Runtime config line $lineNumber contains an embedded control character."
        }
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "Runtime config line $lineNumber is malformed."
        }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($name -notmatch '^[A-Z][A-Z0-9_]*$') {
            throw "Runtime config line $lineNumber has an invalid key name."
        }
        if (-not $schema.ContainsKey($name)) {
            throw "Runtime config line $lineNumber uses an unknown key: $name."
        }
        if ($values.ContainsKey($name)) {
            throw "Runtime config contains a duplicate key: $name."
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Runtime config key $name must not be empty."
        }
        foreach ($character in $value.ToCharArray()) {
            if ([char]::IsControl($character)) {
                throw "Runtime config key $name contains a control character."
            }
        }
        $values.Add($name, $value)
    }
    return $values
}

function Protect-MeetingAiSecret {
    param([Parameter(Mandatory = $true)][string]$PlainText)

    if ([string]::IsNullOrWhiteSpace($PlainText)) {
        throw "Secret value must not be empty."
    }
    $plainBytes = [Text.Encoding]::UTF8.GetBytes($PlainText)
    try {
        $protected = [System.Security.Cryptography.ProtectedData]::Protect(
            $plainBytes,
            $script:MeetingAiDpapiEntropy,
            [System.Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        try {
            return [Convert]::ToBase64String($protected)
        } finally {
            [Array]::Clear($protected, 0, $protected.Length)
        }
    } finally {
        [Array]::Clear($plainBytes, 0, $plainBytes.Length)
    }
}

function Unprotect-MeetingAiSecret {
    param(
        [Parameter(Mandatory = $true)][string]$ProtectedBase64,
        [Parameter(Mandatory = $true)][string]$KeyName
    )

    try {
        $protected = [Convert]::FromBase64String($ProtectedBase64)
    } catch {
        throw "Runtime config key $KeyName is not valid base64."
    }
    try {
        $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $protected,
            $script:MeetingAiDpapiEntropy,
            [System.Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        try {
            $value = (New-Object Text.UTF8Encoding($false, $true)).GetString($plain)
        } finally {
            [Array]::Clear($plain, 0, $plain.Length)
        }
    } catch {
        throw "Runtime config key $KeyName cannot be decrypted on this machine."
    } finally {
        [Array]::Clear($protected, 0, $protected.Length)
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Runtime config key $KeyName decrypts to an empty value."
    }
    return $value
}

function Assert-MeetingAiConfigValues {
    param([Parameter(Mandatory = $true)]$Values)

    if (-not $Values.ContainsKey("MAI_INGESTION_ENABLED")) {
        throw "Runtime config is missing MAI_INGESTION_ENABLED."
    }
    $enabled = $Values["MAI_INGESTION_ENABLED"].ToLowerInvariant()
    if ($enabled -notin @("true", "false")) {
        throw "MAI_INGESTION_ENABLED must be exactly true or false."
    }
    if ($enabled -eq "false") { return }
    if ($Values.ContainsKey("MAI_APP_ENV") -and
        $Values["MAI_APP_ENV"].ToLowerInvariant() -notin @("test", "stage", "prod")) {
        throw "MAI_APP_ENV must be exactly test, stage, or prod."
    }

    $schema = Get-MeetingAiConfigSchema
    foreach ($name in $schema.Keys) {
        if ($schema[$name].Required -and -not $Values.ContainsKey($name)) {
            throw "Runtime config is missing required key: $name."
        }
    }

    foreach ($urlName in @("MAI_MEETING_SERVICE_BASE_URL", "MAI_MEETING_SERVICE_TOKEN_URL")) {
        $uri = $null
        if (-not [Uri]::TryCreate($Values[$urlName], [UriKind]::Absolute, [ref]$uri) -or
            $uri.Scheme -ne "https") {
            throw "$urlName must be an absolute HTTPS URL."
        }
    }
    $tlsMode = $Values["MAI_MEETING_SERVICE_TLS_MODE"].ToLowerInvariant()
    if ($tlsMode -notin @("server", "mutual")) {
        throw "MAI_MEETING_SERVICE_TLS_MODE must be exactly server or mutual."
    }
    if ($tlsMode -eq "mutual") {
        foreach ($name in @(
                "MAI_MEETING_SERVICE_TLS_CA_PATH",
                "MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH",
                "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"
            )) {
            if (-not $Values.ContainsKey($name) -or
                [string]::IsNullOrWhiteSpace($Values[$name])) {
                throw "Mutual TLS runtime config is missing required key: $name."
            }
        }
    }
    foreach ($pathName in @(
            "MAI_MEETING_SERVICE_TLS_CA_PATH",
            "MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH"
        )) {
        if (-not $Values.ContainsKey($pathName)) { continue }
        $tlsPath = Assert-MeetingAiRuntimePath -Path $Values[$pathName] `
            -Purpose "Meeting-service TLS material"
        if (-not (Test-Path -LiteralPath $tlsPath -PathType Leaf)) {
            throw "$pathName must reference an existing TLS material file."
        }
        Assert-MeetingAiAcl -Path $tlsPath
    }
    [void](Assert-MeetingAiRuntimePath -Path $Values["MAI_INGESTION_STORE_PATH"] `
        -Purpose "Analysis delivery store")

    $readyEnabled = "false"
    if ($Values.ContainsKey("MAI_READY_CONSUMER_ENABLED")) {
        $readyEnabled = $Values["MAI_READY_CONSUMER_ENABLED"].ToLowerInvariant()
        if ($readyEnabled -notin @("true", "false")) {
            throw "MAI_READY_CONSUMER_ENABLED must be exactly true or false."
        }
    }
    if ($readyEnabled -eq "false") { return }

    $readyRequired = @(
        "MAI_READY_REDIS_URL_DPAPI",
        "MAI_READY_REDIS_STREAM",
        "MAI_READY_REDIS_GROUP",
        "MAI_READY_PRODUCER_REPLAY_HORIZON_SEC",
        "MAI_TRANSCRIPT_SERVICE_BASE_URL",
        "MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE",
        "MAI_TRANSCRIPT_SERVICE_CAPABILITY_PATH_TEMPLATE",
        "MAI_TRANSCRIPT_SERVICE_TOKEN_URL",
        "MAI_TRANSCRIPT_SERVICE_CLIENT_ID",
        "MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI",
        "MAI_TRANSCRIPT_SERVICE_AUDIENCE",
        "MAI_TRANSCRIPT_SERVICE_SCOPE",
        "MAI_TRANSCRIPT_SERVICE_CAPABILITY_SCOPE",
        "MAI_READY_PRE_ENABLE_PERMIT_PATH",
        "MAI_READY_EXPECTED_GITOPS_COMMIT",
        "MAI_READY_EXPECTED_POLICY_SHA256",
        "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST"
    )
    foreach ($name in $readyRequired) {
        if (-not $Values.ContainsKey($name) -or
            [string]::IsNullOrWhiteSpace($Values[$name])) {
            throw "Ready consumer runtime config is missing required key: $name."
        }
    }
    foreach ($urlName in @(
            "MAI_TRANSCRIPT_SERVICE_BASE_URL",
            "MAI_TRANSCRIPT_SERVICE_TOKEN_URL"
        )) {
        $uri = $null
        if (-not [Uri]::TryCreate($Values[$urlName], [UriKind]::Absolute, [ref]$uri) -or
            $uri.Scheme -ne "https" -or -not [string]::IsNullOrWhiteSpace($uri.UserInfo)) {
            throw "$urlName must be an absolute HTTPS URL without embedded credentials."
        }
    }
    if ($Values["MAI_TRANSCRIPT_SERVICE_SCOPE"] -ne "transcript:canonical:read") {
        throw "MAI_TRANSCRIPT_SERVICE_SCOPE must be exactly transcript:canonical:read."
    }
    if ($Values["MAI_TRANSCRIPT_SERVICE_CAPABILITY_SCOPE"] -ne
        "transcript:analysis-job-capability:issue") {
        throw "MAI_TRANSCRIPT_SERVICE_CAPABILITY_SCOPE has an unexpected permission."
    }
    if ($Values["MAI_READY_EXPECTED_GITOPS_COMMIT"] -notmatch '^[0-9a-f]{40}$') {
        throw "MAI_READY_EXPECTED_GITOPS_COMMIT must be a lowercase full Git SHA."
    }
    if ($Values["MAI_READY_EXPECTED_POLICY_SHA256"] -notmatch '^[0-9a-f]{64}$') {
        throw "MAI_READY_EXPECTED_POLICY_SHA256 must be a lowercase SHA-256 digest."
    }
    if ($Values["MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST"] -notmatch
        '^sha256:[0-9a-f]{64}$') {
        throw "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST must be an immutable image digest."
    }
    $permitPath = Assert-MeetingAiRuntimePath `
        -Path $Values["MAI_READY_PRE_ENABLE_PERMIT_PATH"] `
        -Purpose "Transcript-ready pre-enable permit"
    if (-not (Test-Path -LiteralPath $permitPath -PathType Leaf)) {
        throw "MAI_READY_PRE_ENABLE_PERMIT_PATH must reference an existing permit file."
    }
    Assert-MeetingAiAcl -Path $permitPath
}

function Assert-MeetingAiKeyring {
    param(
        [Parameter(Mandatory = $true)][string]$KeyringJson,
        [Parameter(Mandatory = $true)][string]$ActiveKeyId
    )

    try {
        $keyring = $KeyringJson | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "The decrypted ingestion keyring is not valid JSON."
    }
    $properties = @($keyring.PSObject.Properties)
    if ($properties.Count -lt 1) {
        throw "The decrypted ingestion keyring is empty."
    }
    $activeFound = $false
    foreach ($property in $properties) {
        if ($property.Name -eq $ActiveKeyId) { $activeFound = $true }
        try {
            $keyBytes = [Convert]::FromBase64String([string]$property.Value)
        } catch {
            throw "The decrypted ingestion keyring contains invalid base64."
        }
        try {
            if ($keyBytes.Length -ne 32) {
                throw "The decrypted ingestion keyring contains a non-AES-256 key."
            }
        } finally {
            [Array]::Clear($keyBytes, 0, $keyBytes.Length)
        }
    }
    if (-not $activeFound) {
        throw "MAI_INGESTION_ACTIVE_KEY_ID is absent from the decrypted keyring."
    }
}

function Import-MeetingAiRuntimeEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Optional
    )

    $full = Assert-MeetingAiRuntimePath -Path $Path -Purpose "Runtime config"
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        if ($Optional) { return $false }
        throw "Runtime config file does not exist."
    }
    $values = Read-MeetingAiConfigFile -Path $full
    Assert-MeetingAiConfigValues -Values $values

    $resolvedSecrets = @{}
    if ($values["MAI_INGESTION_ENABLED"].ToLowerInvariant() -eq "true") {
        $resolvedSecrets["MAI_MEETING_SERVICE_CLIENT_SECRET"] =
            Unprotect-MeetingAiSecret `
                -ProtectedBase64 $values["MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"] `
                -KeyName "MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"
        $resolvedSecrets["MAI_INGESTION_ENCRYPTION_KEYS_JSON"] =
            Unprotect-MeetingAiSecret `
                -ProtectedBase64 $values["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
                -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
        Assert-MeetingAiKeyring `
            -KeyringJson $resolvedSecrets["MAI_INGESTION_ENCRYPTION_KEYS_JSON"] `
            -ActiveKeyId $values["MAI_INGESTION_ACTIVE_KEY_ID"]

        $storeParent = Split-Path -Parent $values["MAI_INGESTION_STORE_PATH"]
        Assert-MeetingAiAcl -Path $storeParent -Directory

        if ($values["MAI_MEETING_SERVICE_TLS_MODE"].ToLowerInvariant() -eq "mutual") {
            $clientKey = Unprotect-MeetingAiSecret `
                -ProtectedBase64 $values["MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"] `
                -KeyName "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"
            try {
                $runtimeKeyPath = Join-Path (Get-MeetingAiRuntimeRoot) `
                    "runtime\meeting-service-client.key"
                Write-MeetingAiSecretFileAtomic -Path $runtimeKeyPath -Content $clientKey
                [Environment]::SetEnvironmentVariable(
                    "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_PATH",
                    $runtimeKeyPath,
                    "Process"
                )
            } finally {
                $clientKey = $null
            }
        } else {
            Clear-MeetingAiRuntimeTlsKey
        }

        if ($values.ContainsKey("MAI_READY_CONSUMER_ENABLED") -and
            $values["MAI_READY_CONSUMER_ENABLED"].ToLowerInvariant() -eq "true") {
            $resolvedSecrets["MAI_READY_REDIS_URL"] =
                Unprotect-MeetingAiSecret `
                    -ProtectedBase64 $values["MAI_READY_REDIS_URL_DPAPI"] `
                    -KeyName "MAI_READY_REDIS_URL_DPAPI"
            $resolvedSecrets["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET"] =
                Unprotect-MeetingAiSecret `
                    -ProtectedBase64 $values["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"] `
                    -KeyName "MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"
        }
    }

    $schema = Get-MeetingAiConfigSchema
    foreach ($name in $values.Keys) {
        if (-not [string]::IsNullOrWhiteSpace($schema[$name].SecretTarget)) { continue }
        [Environment]::SetEnvironmentVariable($name, $values[$name], "Process")
    }
    foreach ($name in $resolvedSecrets.Keys) {
        [Environment]::SetEnvironmentVariable($name, $resolvedSecrets[$name], "Process")
    }
    return $true
}

function Get-MeetingAiFileSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Purpose file does not exist."
    }
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") })
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Assert-TranscriptReadyPreEnablePermit {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$StartupScriptPath
    )

    if ($env:MAI_READY_CONSUMER_ENABLED -ne "true") { return }

    foreach ($name in @(
            "MAI_READY_PRE_ENABLE_PERMIT_PATH",
            "MAI_READY_EXPECTED_GITOPS_COMMIT",
            "MAI_READY_EXPECTED_POLICY_SHA256",
            "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST"
        )) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            throw "Transcript-ready startup permit binding is incomplete."
        }
    }

    $permitPath = Assert-MeetingAiRuntimePath `
        -Path $env:MAI_READY_PRE_ENABLE_PERMIT_PATH `
        -Purpose "Transcript-ready pre-enable permit"
    Assert-MeetingAiAcl -Path $permitPath
    $bytes = [IO.File]::ReadAllBytes($permitPath)
    if ($bytes.Length -lt 2 -or $bytes.Length -gt 1048576) {
        throw "Transcript-ready pre-enable permit has an invalid size."
    }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and
        $bytes[2] -eq 0xBF) {
        throw "Transcript-ready pre-enable permit must be UTF-8 without BOM."
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $json = $utf8.GetString($bytes)
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    if ($json.IndexOf([char]0) -ge 0) {
        throw "Transcript-ready pre-enable permit contains a NUL character."
    }
    try {
        $permit = $json | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Transcript-ready pre-enable permit is not valid JSON."
    } finally {
        $json = $null
    }

    if ($permit.schemaVersion -ne "faz24.transcriptReadyPreEnableVerdict.v1" -or
        $permit.issue -ne "platform-k8s-gitops#2610" -or
        $permit.status -ne "accepted-candidate" -or
        $permit.enableAuthorized -ne $true) {
        throw "Transcript-ready pre-enable permit verdict is not accepted."
    }
    if (@($permit.requiredRemediationEvidence).Count -ne 0 -or
        @($permit.checks).Count -lt 1 -or
        @($permit.checks | Where-Object { $_.passed -ne $true }).Count -ne 0) {
        throw "Transcript-ready pre-enable permit contains a failed or pending check."
    }

    $generatedAt = [DateTimeOffset]::MinValue
    $dateStyles = [Globalization.DateTimeStyles]::AssumeUniversal -bor
        [Globalization.DateTimeStyles]::AdjustToUniversal
    if (-not [DateTimeOffset]::TryParse(
            [string]$permit.generatedAt,
            [Globalization.CultureInfo]::InvariantCulture,
            $dateStyles,
            [ref]$generatedAt
        )) {
        throw "Transcript-ready pre-enable permit has an invalid generation time."
    }
    $now = [DateTimeOffset]::UtcNow
    $ageSeconds = ($now - $generatedAt).TotalSeconds
    if ($ageSeconds -lt -30 -or $ageSeconds -gt 900) {
        throw "Transcript-ready pre-enable permit is outside the 900 second startup window."
    }

    $binding = $permit.binding
    if ($null -eq $binding -or
        $binding.expectedGitopsCommit -ne $env:MAI_READY_EXPECTED_GITOPS_COMMIT -or
        $binding.policySha256 -ne $env:MAI_READY_EXPECTED_POLICY_SHA256) {
        throw "Transcript-ready pre-enable permit GitOps binding does not match."
    }
    $producer = $binding.producerCapability
    if ($null -eq $producer -or
        $producer.transcriptImageDigest -ne
            $env:MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST) {
        throw "Transcript-ready pre-enable permit producer binding does not match."
    }

    $repoFull = Resolve-FixedLocalPath -Path $RepoRoot -Purpose "Platform-ai repository"
    $repoCommit = (& git -C $repoFull rev-parse HEAD 2>$null).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $repoCommit -notmatch '^[0-9a-f]{40}$') {
        throw "Platform-ai repository identity could not be read."
    }
    $startupFull = Resolve-FixedLocalPath `
        -Path $StartupScriptPath -Purpose "Meeting-ai startup script"
    $startupSha256 = Get-MeetingAiFileSha256 `
        -Path $startupFull -Purpose "Meeting-ai startup script"
    $hostGuard = $binding.hostStartupGuard
    if ($null -eq $hostGuard -or
        $hostGuard.permitRequired -ne $true -or
        $hostGuard.platformAiCommit -ne $repoCommit -or
        $hostGuard.startupScriptSha256 -ne $startupSha256) {
        throw "Transcript-ready pre-enable permit host binding does not match."
    }
}

function Get-MeetingAiRuntimeTlsKeyPath {
    return Join-Path (Get-MeetingAiRuntimeRoot) "runtime\meeting-service-client.key"
}

function Clear-MeetingAiRuntimeTlsKey {
    $runtimeKeyPath = Get-MeetingAiRuntimeTlsKeyPath
    if (Test-Path -LiteralPath $runtimeKeyPath -PathType Leaf) {
        Remove-Item -LiteralPath $runtimeKeyPath -Force
    }
    [Environment]::SetEnvironmentVariable(
        "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_PATH",
        $null,
        "Process"
    )
}

function Write-MeetingAiSecretFileAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    if ([string]::IsNullOrWhiteSpace($Content)) {
        throw "Runtime secret material must not be empty."
    }
    $full = Assert-MeetingAiRuntimePath -Path $Path -Purpose "Runtime TLS key"
    $directory = Initialize-MeetingAiDirectory -Path (Split-Path -Parent $full)
    $temp = Join-Path $directory (".meeting-ai-key-{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllBytes($temp, @())
        Set-Acl -LiteralPath $temp -AclObject (New-MeetingAiAcl)
        [IO.File]::WriteAllText($temp, $Content, (New-Object Text.UTF8Encoding($false)))
        Assert-MeetingAiAcl -Path $temp
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            # .NET Framework File.Replace rejects a null backup path on PS 5.1.
            # MoveFileEx keeps same-volume replacement atomic without creating a
            # second plaintext private-key copy.
            $replaceExistingAndWriteThrough = 0x1 -bor 0x8
            if (-not [MeetingAi.NativeMethods]::MoveFileEx(
                    $temp,
                    $full,
                    $replaceExistingAndWriteThrough
                )) {
                $win32Error = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                throw "Runtime secret atomic replacement failed (Win32 $win32Error)."
            }
        } else {
            [IO.File]::Move($temp, $full)
        }
        Set-Acl -LiteralPath $full -AclObject (New-MeetingAiAcl)
        Assert-MeetingAiAcl -Path $full
    } finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force
        }
    }
}

function Write-MeetingAiConfigAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $full = Assert-MeetingAiRuntimePath -Path $Path -Purpose "Runtime config"
    [void](Initialize-MeetingAiRuntimeRoot)
    $directory = Initialize-MeetingAiDirectory -Path (Split-Path -Parent $full)
    $temp = Join-Path $directory (".meeting-ai-{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
    $backup = "$full.bak"
    try {
        [IO.File]::WriteAllBytes($temp, @())
        Set-Acl -LiteralPath $temp -AclObject (New-MeetingAiAcl)
        $encoding = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temp, $Content, $encoding)
        Assert-MeetingAiAcl -Path $temp
        $candidate = Read-MeetingAiConfigFile -Path $temp
        Assert-MeetingAiConfigValues -Values $candidate
        if ($candidate["MAI_INGESTION_ENABLED"].ToLowerInvariant() -eq "true") {
            $candidateClientSecret = Unprotect-MeetingAiSecret `
                -ProtectedBase64 $candidate["MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"] `
                -KeyName "MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"
            $candidateKeyring = Unprotect-MeetingAiSecret `
                -ProtectedBase64 $candidate["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
                -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
            Assert-MeetingAiKeyring -KeyringJson $candidateKeyring `
                -ActiveKeyId $candidate["MAI_INGESTION_ACTIVE_KEY_ID"]
            if ($candidate["MAI_MEETING_SERVICE_TLS_MODE"].ToLowerInvariant() -eq
                "mutual") {
                $candidateClientKey = Unprotect-MeetingAiSecret `
                    -ProtectedBase64 $candidate["MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"] `
                    -KeyName "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"
                $candidateClientKey = $null
            }
            $candidateClientSecret = $null
            $candidateKeyring = $null
        }

        if (Test-Path -LiteralPath $full) {
            [IO.File]::Replace($temp, $full, $backup, $true)
            Set-Acl -LiteralPath $backup -AclObject (New-MeetingAiAcl)
            Assert-MeetingAiAcl -Path $backup
        } else {
            [IO.File]::Move($temp, $full)
        }
        Set-Acl -LiteralPath $full -AclObject (New-MeetingAiAcl)
        Assert-MeetingAiAcl -Path $full
    } finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force
        }
    }
}
