# Provision or rotate meeting-ai durable-delivery runtime configuration.
# Run from elevated Windows PowerShell 5.1. Secret values are prompted as a
# SecureString and stored as DPAPI LocalMachine ciphertext, never plaintext.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$MeetingServiceBaseUrl = "",
    [string]$MeetingServiceTokenUrl = "",
    [string]$ClientId = "meeting-ai",
    [Security.SecureString]$ClientSecret,
    [string]$Audience = "meeting-service",
    [string]$Permission = "meeting:analysis-result:write",
    [ValidateSet("", "server", "mutual")][string]$TlsMode = "",
    [string]$TlsCaPath = "",
    [string]$TlsClientCertPath = "",
    [string]$TlsClientKeyPath = "",
    [ValidateSet("", "test", "stage", "prod")][string]$RuntimeAppEnv = "",
    [ValidateSet("", "true", "false")][string]$ReadyConsumerEnabled = "",
    [Security.SecureString]$ReadyRedisUrl,
    [string]$ReadyRedisStream = "",
    [string]$ReadyRedisGroup = "",
    [double]$ReadyProducerReplayHorizonSec = 0,
    [string]$AnalysisSpecVersion = "",
    [string]$TranscriptServiceBaseUrl = "",
    [string]$TranscriptServiceSnapshotPathTemplate = "",
    [string]$TranscriptServiceCapabilityPathTemplate = "",
    [string]$TranscriptServiceTokenUrl = "",
    [string]$TranscriptServiceClientId = "",
    [Security.SecureString]$TranscriptServiceClientSecret,
    [string]$ReadyPermitSourcePath = "",
    [string]$ReadyPermitTrustRootSourcePath = "",
    [string]$ExpectedGitopsCommit = "",
    [string]$ExpectedPolicySha256 = "",
    [string]$ExpectedProducerImageDigest = "",
    [string]$ExpectedPermitTrustRootSha256 = "",
    [string]$PythonExe = "python",
    [string]$StorePath = "",
    [string]$ConfigPath = "",
    [switch]$RotateEncryptionKey,
    [switch]$RestoreBackup
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated Administrator PowerShell."
}

$scriptDir = Split-Path $PSCommandPath -Parent
. (Join-Path $scriptDir "meeting-ai-runtime-env.ps1")

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $env:ProgramData "Acik\platform-ai\meeting-ai.env"
}
if ([string]::IsNullOrWhiteSpace($StorePath)) {
    $StorePath = Join-Path $env:ProgramData `
        "Acik\platform-ai\meeting-ai\analysis-delivery.sqlite3"
}

$ConfigPath = Assert-MeetingAiRuntimePath -Path $ConfigPath -Purpose "Runtime config"
$StorePath = Assert-MeetingAiRuntimePath -Path $StorePath -Purpose "Analysis delivery store"
$operation = if ($RestoreBackup) {
    "restore DPAPI-protected meeting-ai config backup"
} else {
    "write DPAPI-protected meeting-ai config"
}
if (-not $PSCmdlet.ShouldProcess($ConfigPath, $operation)) {
    return
}
$mutex = New-Object Threading.Mutex($false, "Global\platform-ai-meeting-ai-config-v1")
$lockTaken = $false
$stagedPermitPath = ""
$stagedPermitTrustRootPath = ""
$stagedActivationReceiptPath = ""
$stagedTlsPublicPaths = @()
$tlsPublicArtifactsCommitted = $false
$previousPermitPath = ""
$previousActivationReceiptPath = ""
$activationCommitted = $false

function Get-ExistingValue {
    param(
        $Existing,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Supplied = ""
    )
    if (-not [string]::IsNullOrWhiteSpace($Supplied)) { return $Supplied }
    if ($null -ne $Existing -and $Existing.ContainsKey($Name)) { return $Existing[$Name] }
    throw "First-time provisioning requires $Name."
}

function Get-SuppliedOrExistingValue {
    param(
        $Existing,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Supplied = ""
    )
    if (-not [string]::IsNullOrWhiteSpace($Supplied)) { return $Supplied }
    if ($null -ne $Existing -and $Existing.ContainsKey($Name)) {
        return $Existing[$Name]
    }
    return ""
}

function Get-ReadyConfiguredValue {
    param(
        $Existing,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Supplied = "",
        [Parameter(Mandatory = $true)][string]$InitialDefault
    )
    if (-not [string]::IsNullOrWhiteSpace($Supplied)) { return $Supplied }
    if ($null -ne $Existing -and $Existing.ContainsKey($Name)) {
        return $Existing[$Name]
    }
    return $InitialDefault
}

function Test-MeetingAiExactLegacyUpgradeShape {
    param([Parameter(Mandatory = $true)]$Values)

    if (-not $Values.ContainsKey("MAI_INGESTION_ENABLED") -or
        $Values["MAI_INGESTION_ENABLED"].ToLowerInvariant() -ne "true") {
        return $false
    }
    foreach ($name in $Values.Keys) {
        if ($name -eq "MAI_APP_ENV" -or
            $name -eq "MAI_INGESTION_LOOKUP_KEY_ID" -or
            $name -eq "MAI_ANALYSIS_SPEC_VERSION" -or
            $name.StartsWith("MAI_READY_", [StringComparison]::OrdinalIgnoreCase) -or
            $name.StartsWith("MAI_TRANSCRIPT_", [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }
    return $true
}

function Assert-MeetingAiExactLegacyUpgradeConfig {
    param([Parameter(Mandatory = $true)]$Values)

    if (-not (Test-MeetingAiExactLegacyUpgradeShape -Values $Values)) {
        throw "Only an exact pre-ready-consumer runtime config may use the legacy upgrade path."
    }
    Assert-MeetingAiConfigValues -Values $Values -AllowLegacyUpgrade

    $clientSecret = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $Values["MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"] `
        -KeyName "MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"
    $keyringJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $Values["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    try {
        Assert-MeetingAiKeyring -KeyringJson $keyringJson `
            -ActiveKeyId $Values["MAI_INGESTION_ACTIVE_KEY_ID"]
        if ($Values["MAI_MEETING_SERVICE_TLS_MODE"].ToLowerInvariant() -eq "mutual") {
            $clientKey = Unprotect-MeetingAiSecret `
                -ProtectedBase64 $Values["MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"] `
                -KeyName "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"
        }
    } finally {
        $clientSecret = $null
        $clientKey = $null
        $keyringJson = $null
    }
}

function Protect-SuppliedSecureValue {
    param([Parameter(Mandatory = $true)][Security.SecureString]$Value)

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        return Protect-MeetingAiSecret -PlainText $plain
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $plain = $null
    }
}

function Assert-MeetingAiReadyRedisEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$ProtectedRedisUrl,
        [Parameter(Mandatory = $true)][string]$PythonExe
    )

    $plainRedisUrl = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $ProtectedRedisUrl `
        -KeyName "MAI_READY_REDIS_URL_DPAPI"
    $previousRedisUrl = [Environment]::GetEnvironmentVariable(
        "MAI_READY_REDIS_PREFLIGHT_URL",
        "Process"
    )
    try {
        [Environment]::SetEnvironmentVariable(
            "MAI_READY_REDIS_PREFLIGHT_URL",
            $plainRedisUrl,
            "Process"
        )
        $probe = @'
import os
import redis

url = os.environ.pop("MAI_READY_REDIS_PREFLIGHT_URL")
client = redis.Redis.from_url(
    url,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=0,
)
try:
    if client.ping() is not True:
        raise RuntimeError("redis ping did not return PONG")
finally:
    client.close()
'@
        try {
            & $PythonExe -c $probe 1>$null 2>$null
        } catch {
            throw "Ready Redis endpoint preflight failed."
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Ready Redis endpoint preflight failed."
        }
    } finally {
        [Environment]::SetEnvironmentVariable(
            "MAI_READY_REDIS_PREFLIGHT_URL",
            $previousRedisUrl,
            "Process"
        )
        $plainRedisUrl = $null
    }
}

function Install-MeetingAiTlsPublicFile {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath,
        [Parameter(Mandatory = $true)][string]$ExpectedMarker
    )

    $source = Resolve-FixedLocalPath -Path $SourcePath -Purpose "TLS source material"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "TLS source material does not exist."
    }
    $item = Get-Item -LiteralPath $source -Force
    if ($item.Length -gt 262144) {
        throw "TLS source material exceeds the 256 KiB limit."
    }
    $text = [IO.File]::ReadAllText(
        $source,
        (New-Object Text.UTF8Encoding($false, $true))
    )
    if (-not $text.Contains($ExpectedMarker)) {
        throw "TLS source material has an unexpected PEM type."
    }
    Write-MeetingAiSecretFileAtomic -Path $DestinationPath -Content $text
    return (Assert-MeetingAiRuntimePath -Path $DestinationPath `
        -Purpose "Installed TLS material")
}

function ConvertTo-MeetingAiConfigContent {
    param([Parameter(Mandatory = $true)]$Values)

    $lines = @(
        "# platform-ai meeting-ai runtime config v1"
        "# Secret fields are DPAPI LocalMachine ciphertext. Do not copy to another host."
    )
    foreach ($name in $Values.Keys) {
        $lines += "{0}={1}" -f $name, $Values[$name]
    }
    return ($lines -join "`r`n") + "`r`n"
}

function Merge-MeetingAiRestoreKeyring {
    param(
        [Parameter(Mandatory = $true)]$BackupValues,
        [Parameter(Mandatory = $true)]$CurrentValues
    )

    $lookupName = "MAI_INGESTION_LOOKUP_KEY_ID"
    $keyringName = "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    if (-not $BackupValues.ContainsKey($lookupName) -or
        -not $BackupValues.ContainsKey($keyringName) -or
        -not $CurrentValues.ContainsKey($lookupName) -or
        -not $CurrentValues.ContainsKey($keyringName)) {
        throw "Runtime config restore requires complete ingestion keyring metadata."
    }
    if ($BackupValues[$lookupName] -ne $CurrentValues[$lookupName]) {
        throw "Runtime config restore cannot cross an unversioned blind-index key change."
    }

    $backupJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $BackupValues[$keyringName] -KeyName $keyringName
    $currentJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $CurrentValues[$keyringName] -KeyName $keyringName
    try {
        $backupKeyring = $backupJson | ConvertFrom-Json -ErrorAction Stop
        $currentKeyring = $currentJson | ConvertFrom-Json -ErrorAction Stop
        $merged = [ordered]@{}
        foreach ($property in $backupKeyring.PSObject.Properties) {
            $merged[$property.Name] = [string]$property.Value
        }
        foreach ($property in $currentKeyring.PSObject.Properties) {
            $keyId = $property.Name
            $keyMaterial = [string]$property.Value
            if ($merged.Contains($keyId) -and $merged[$keyId] -ne $keyMaterial) {
                throw "Runtime config restore found conflicting material for key id '$keyId'."
            }
            $merged[$keyId] = $keyMaterial
        }
        $mergedJson = $merged | ConvertTo-Json -Compress
        Assert-MeetingAiKeyring -KeyringJson $mergedJson `
            -ActiveKeyId $BackupValues["MAI_INGESTION_ACTIVE_KEY_ID"] `
            -LookupKeyId $BackupValues[$lookupName]
        $BackupValues[$keyringName] = Protect-MeetingAiSecret -PlainText $mergedJson
    } finally {
        $backupJson = $null
        $currentJson = $null
        $backupKeyring = $null
        $currentKeyring = $null
        $merged = $null
        $mergedJson = $null
        $keyMaterial = $null
    }
}

function Write-MeetingAiExclusiveRuntimeFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $full = Assert-MeetingAiRuntimePath -Path $Path -Purpose "Runtime consumption record"
    [void](Initialize-MeetingAiDirectory -Path (Split-Path -Parent $full))
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $full,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Content)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally {
            [Array]::Clear($bytes, 0, $bytes.Length)
        }
    } catch [IO.IOException] {
        throw "Transcript-ready pre-enable permit was already consumed."
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    Set-Acl -LiteralPath $full -AclObject (New-MeetingAiAcl)
    Assert-MeetingAiAcl -Path $full
}

function New-TranscriptReadyActivation {
    param(
        [Parameter(Mandatory = $true)][string]$PermitSourcePath,
        [Parameter(Mandatory = $true)][string]$PermitTrustRootSourcePath,
        [ValidateSet("test", "stage", "prod")][string]$TargetAppEnv,
        [Parameter(Mandatory = $true)][string]$ExpectedGitopsCommit,
        [Parameter(Mandatory = $true)][string]$ExpectedPolicySha256,
        [Parameter(Mandatory = $true)][string]$ExpectedProducerImageDigest,
        [Parameter(Mandatory = $true)][string]$ExpectedTrustRootSha256,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$StartupScriptPath,
        [Parameter(Mandatory = $true)][string]$PythonExe
    )

    $source = Resolve-FixedLocalPath `
        -Path $PermitSourcePath -Purpose "Transcript-ready pre-enable permit source"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
        (Get-Item -LiteralPath $source -Force).Length -gt 1048576) {
        throw "Transcript-ready pre-enable permit source is missing or too large."
    }
    $trustRootSource = Resolve-FixedLocalPath `
        -Path $PermitTrustRootSourcePath `
        -Purpose "Transcript-ready permit trust-root source"
    if (-not (Test-Path -LiteralPath $trustRootSource -PathType Leaf) -or
        (Get-Item -LiteralPath $trustRootSource -Force).Length -lt 2 -or
        (Get-Item -LiteralPath $trustRootSource -Force).Length -gt 1048576) {
        throw "Transcript-ready permit trust-root source is missing or too large."
    }
    if ($ExpectedTrustRootSha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw "Transcript-ready expected trust-root fingerprint is invalid."
    }
    $trustRootSha256 = Get-MeetingAiFileSha256 `
        -Path $trustRootSource -Purpose "Transcript-ready permit trust root"
    if ($trustRootSha256 -ne $ExpectedTrustRootSha256) {
        throw "Transcript-ready permit trust-root fingerprint does not match."
    }
    $trustRootContent = [IO.File]::ReadAllText(
        $trustRootSource,
        (New-Object Text.UTF8Encoding($false, $true))
    )
    $trustRootPath = Join-Path (Get-MeetingAiRuntimeRoot) `
        ("permits\trust\transcript-ready-trust-root-{0}.json" -f $trustRootSha256)
    $trustRootCreated = -not (Test-Path -LiteralPath $trustRootPath -PathType Leaf)
    if ($trustRootCreated) {
        Write-MeetingAiSecretFileAtomic `
            -Path $trustRootPath -Content $trustRootContent `
            -Purpose "Transcript-ready permit trust root"
    } elseif ((Get-MeetingAiFileSha256 `
            -Path $trustRootPath -Purpose "Installed transcript-ready permit trust root") -ne
        $trustRootSha256) {
        throw "Installed transcript-ready permit trust root does not match its content address."
    }
    Assert-MeetingAiAcl -Path $trustRootPath
    $consumingPath = Join-Path (Split-Path -Parent $source) `
        (".{0}.consuming-{1}" -f (Split-Path -Leaf $source), [Guid]::NewGuid().ToString("N"))
    [IO.File]::Move($source, $consumingPath)
    $permitPath = ""
    $receiptPath = ""
    $permitContent = $null
    try {
        $permitBytes = [IO.File]::ReadAllBytes($consumingPath)
        $hasher = [Security.Cryptography.SHA256]::Create()
        $hashBytes = $null
        try {
            $permitContent = (New-Object Text.UTF8Encoding($false, $true)).GetString(
                $permitBytes
            )
            $hashBytes = $hasher.ComputeHash($permitBytes)
            $permitSha256 = ([BitConverter]::ToString($hashBytes)).Replace(
                "-", ""
            ).ToLowerInvariant()
        } finally {
            $hasher.Dispose()
            if ($null -ne $hashBytes) { [Array]::Clear($hashBytes, 0, $hashBytes.Length) }
            [Array]::Clear($permitBytes, 0, $permitBytes.Length)
        }

        $consumptionPath = Join-Path (Get-MeetingAiRuntimeRoot) `
            ("permits\consumed\{0}.json" -f $permitSha256)
        $consumption = [ordered]@{
            schemaVersion = "faz24.transcriptReadyPermitConsumption.v1"
            permitSha256 = $permitSha256
            targetAppEnv = $TargetAppEnv
            consumedAt = [DateTimeOffset]::UtcNow.ToString("o")
        }
        Write-MeetingAiExclusiveRuntimeFile `
            -Path $consumptionPath `
            -Content (($consumption | ConvertTo-Json -Depth 4) + "`n")

        $permitPath = Join-Path (Get-MeetingAiRuntimeRoot) `
            ("permits\active\transcript-ready-pre-enable-{0}.json" -f $permitSha256)
        Write-MeetingAiSecretFileAtomic `
            -Path $permitPath -Content $permitContent `
            -Purpose "Transcript-ready pre-enable permit"
        $verifiedPermit = Assert-TranscriptReadyPermitFile `
            -PermitPath $permitPath `
            -TrustRootPath $trustRootPath `
            -ExpectedTrustRootSha256 $ExpectedTrustRootSha256 `
            -ExpectedGitopsCommit $ExpectedGitopsCommit `
            -ExpectedPolicySha256 $ExpectedPolicySha256 `
            -ExpectedProducerImageDigest $ExpectedProducerImageDigest `
            -RepoRoot $RepoRoot `
            -StartupScriptPath $StartupScriptPath `
            -PythonExe $PythonExe `
            -AppEnv $TargetAppEnv

        $headResult = Invoke-MeetingAiGitCapture -GitArgs @(
            "-C", $RepoRoot, "rev-parse", "HEAD"
        )
        if ($headResult.ExitCode -ne 0 -or $headResult.Output.Count -ne 1) {
            throw "Platform-ai repository identity could not be read for activation."
        }
        $platformAiCommit = "$($headResult.Output[0])".Trim().ToLowerInvariant()
        $startupSha256 = Get-MeetingAiFileSha256 `
            -Path $StartupScriptPath -Purpose "Meeting-ai startup script"
        $receipt = [ordered]@{
            schemaVersion = "faz24.transcriptReadyActivationReceipt.v3"
            authorityBoundary = "local-non-authoritative"
            permitEnvelopeSha256 = $verifiedPermit.PermitEnvelopeSha256
            trustRootSha256 = $verifiedPermit.TrustRootSha256
            signingKeyId = $verifiedPermit.SigningKeyId
            targetAppEnv = $TargetAppEnv
            expectedGitopsCommit = $ExpectedGitopsCommit
            policySha256 = $ExpectedPolicySha256
            producerImageDigest = $ExpectedProducerImageDigest
            platformAiCommit = $platformAiCommit
            startupScriptSha256 = $startupSha256
            liveTranscriptPodUid = $verifiedPermit.LiveTranscriptPodUid
            liveTranscriptImageDigest = $verifiedPermit.LiveTranscriptImageDigest
            liveTranscriptObservedAt = $verifiedPermit.LiveTranscriptObservedAt
            liveEvidenceSha256 = $verifiedPermit.LiveEvidenceSha256
            activatedAt = [DateTimeOffset]::UtcNow.ToString("o")
        }
        $receiptPath = Join-Path (Get-MeetingAiRuntimeRoot) `
            ("permits\activations\transcript-ready-activation-{0}.json" -f $permitSha256)
        Write-MeetingAiSecretFileAtomic `
            -Path $receiptPath -Content (($receipt | ConvertTo-Json -Depth 4) + "`n")
        [void](Assert-TranscriptReadyActivationReceiptFile `
            -ReceiptPath $receiptPath `
            -PermitPath $permitPath `
            -TrustRootPath $trustRootPath `
            -ExpectedTrustRootSha256 $ExpectedTrustRootSha256 `
            -ExpectedGitopsCommit $ExpectedGitopsCommit `
            -ExpectedPolicySha256 $ExpectedPolicySha256 `
            -ExpectedProducerImageDigest $ExpectedProducerImageDigest `
            -RepoRoot $RepoRoot `
            -StartupScriptPath $StartupScriptPath `
            -PythonExe $PythonExe `
            -AppEnv $TargetAppEnv)

        return [pscustomobject]@{
            PermitPath = $permitPath
            TrustRootPath = $trustRootPath
            ReceiptPath = $receiptPath
            PermitSha256 = $permitSha256
        }
    } catch {
        foreach ($path in @($permitPath, $receiptPath)) {
            if (-not [string]::IsNullOrWhiteSpace($path) -and
                (Test-Path -LiteralPath $path -PathType Leaf)) {
                Remove-Item -LiteralPath $path -Force
            }
        }
        if ($trustRootCreated -and
            (Test-Path -LiteralPath $trustRootPath -PathType Leaf)) {
            Remove-Item -LiteralPath $trustRootPath -Force
        }
        throw
    } finally {
        $permitContent = $null
        $trustRootContent = $null
        if (Test-Path -LiteralPath $consumingPath -PathType Leaf) {
            Remove-Item -LiteralPath $consumingPath -Force
        }
    }
}

try {
    $lockTaken = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
    if (-not $lockTaken) { throw "Another meeting-ai configuration operation is in progress." }

    [void](Initialize-MeetingAiRuntimeRoot)

    if ($RestoreBackup) {
        $backupPath = "$ConfigPath.bak"
        if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            throw "No meeting-ai runtime config backup exists."
        }
        $currentValues = $null
        if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
            $currentValues = Read-MeetingAiConfigFile -Path $ConfigPath
            Assert-MeetingAiConfigValues -Values $currentValues
        }
        $backupValues = Read-MeetingAiConfigFile -Path $backupPath
        Assert-MeetingAiConfigValues -Values $backupValues -SkipReadyArtifactExistence
        if ($null -ne $currentValues) {
            Merge-MeetingAiRestoreKeyring `
                -BackupValues $backupValues -CurrentValues $currentValues
        }
        if ($backupValues.ContainsKey("MAI_READY_CONSUMER_ENABLED") -and
            $backupValues["MAI_READY_CONSUMER_ENABLED"].ToLowerInvariant() -eq "true") {
            if ([string]::IsNullOrWhiteSpace($ReadyPermitSourcePath) -or
                [string]::IsNullOrWhiteSpace($ReadyPermitTrustRootSourcePath)) {
                throw "Restoring an enabled ready-consumer backup requires a fresh signed permit and trust root."
            }
            Assert-MeetingAiReadyRedisEndpoint `
                -ProtectedRedisUrl $backupValues["MAI_READY_REDIS_URL_DPAPI"] `
                -PythonExe $PythonExe
            $repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
            $startupScript = Join-Path $scriptDir "start-meeting-ai.ps1"
            $activation = New-TranscriptReadyActivation `
                -PermitSourcePath $ReadyPermitSourcePath `
                -PermitTrustRootSourcePath $ReadyPermitTrustRootSourcePath `
                -TargetAppEnv $backupValues["MAI_APP_ENV"].ToLowerInvariant() `
                -ExpectedGitopsCommit $backupValues["MAI_READY_EXPECTED_GITOPS_COMMIT"] `
                -ExpectedPolicySha256 $backupValues["MAI_READY_EXPECTED_POLICY_SHA256"] `
                -ExpectedProducerImageDigest `
                    $backupValues["MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST"] `
                -ExpectedTrustRootSha256 `
                    $backupValues["MAI_READY_EXPECTED_PERMIT_TRUST_ROOT_SHA256"] `
                -RepoRoot $repoRoot -StartupScriptPath $startupScript `
                -PythonExe $PythonExe
            $stagedPermitPath = $activation.PermitPath
            $stagedPermitTrustRootPath = $activation.TrustRootPath
            $stagedActivationReceiptPath = $activation.ReceiptPath
            $backupValues["MAI_READY_PRE_ENABLE_PERMIT_PATH"] = $stagedPermitPath
            $backupValues["MAI_READY_PERMIT_TRUST_ROOT_PATH"] = `
                $stagedPermitTrustRootPath
            $backupValues["MAI_READY_ACTIVATION_RECEIPT_PATH"] = `
                $stagedActivationReceiptPath
        }
        $backupContent = ConvertTo-MeetingAiConfigContent -Values $backupValues
        if ($env:CI -eq "true" -and
            $env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE -eq "1") {
            throw "TEST_INJECTED_MEETING_AI_CONFIG_WRITE_FAILURE"
        }
        Write-MeetingAiConfigAtomic -Path $ConfigPath -Content $backupContent
        if (-not [string]::IsNullOrWhiteSpace($stagedPermitPath)) {
            $activationCommitted = $true
        }
        if ($null -ne $currentValues -and
            $currentValues.ContainsKey("MAI_READY_PRE_ENABLE_PERMIT_PATH")) {
            foreach ($name in @(
                    "MAI_READY_PRE_ENABLE_PERMIT_PATH",
                    "MAI_READY_ACTIVATION_RECEIPT_PATH"
                )) {
                if (-not $currentValues.ContainsKey($name)) { continue }
                $oldArtifact = Assert-MeetingAiRuntimePath `
                    -Path $currentValues[$name] -Purpose "Previous ready activation artifact"
                if ($oldArtifact -ne $stagedPermitPath -and
                    $oldArtifact -ne $stagedActivationReceiptPath -and
                    (Test-Path -LiteralPath $oldArtifact -PathType Leaf)) {
                    Remove-Item -LiteralPath $oldArtifact -Force
                }
            }
        }
        Write-Host "meeting-ai runtime config backup restored: $ConfigPath"
        Write-Host "Restart task with schtasks.exe /End and /Run for platform-ai-meeting-ai."
        return
    }

    $existing = $null
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        $existing = Read-MeetingAiConfigFile -Path $ConfigPath
        if (Test-MeetingAiExactLegacyUpgradeShape -Values $existing) {
            Assert-MeetingAiExactLegacyUpgradeConfig -Values $existing
        } else {
            Assert-MeetingAiConfigValues -Values $existing
        }
    }
    $effectiveAppEnv = Get-SuppliedOrExistingValue -Existing $existing `
        -Name "MAI_APP_ENV" -Supplied $RuntimeAppEnv

    $baseUrl = Get-ExistingValue -Existing $existing `
        -Name "MAI_MEETING_SERVICE_BASE_URL" -Supplied $MeetingServiceBaseUrl
    $tokenUrl = Get-ExistingValue -Existing $existing `
        -Name "MAI_MEETING_SERVICE_TOKEN_URL" -Supplied $MeetingServiceTokenUrl

    $effectiveTlsMode = if (-not [string]::IsNullOrWhiteSpace($TlsMode)) {
        $TlsMode.ToLowerInvariant()
    } elseif ($null -ne $existing -and
        $existing.ContainsKey("MAI_MEETING_SERVICE_TLS_MODE")) {
        $existing["MAI_MEETING_SERVICE_TLS_MODE"].ToLowerInvariant()
    } else {
        "server"
    }
    $tlsRoot = Join-Path (Get-MeetingAiRuntimeRoot) "tls"
    $tlsMaterialVersion = "{0}-{1}" -f `
        (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"),
        ([Guid]::NewGuid().ToString("N").Substring(0, 8))
    $installedCaPath = ""
    $installedCertPath = ""
    $clientKeyBlob = ""

    if (-not [string]::IsNullOrWhiteSpace($TlsCaPath)) {
        $installedCaPath = Install-MeetingAiTlsPublicFile `
            -SourcePath $TlsCaPath `
            -DestinationPath (Join-Path $tlsRoot `
                ("meeting-service-ca-{0}.pem" -f $tlsMaterialVersion)) `
            -ExpectedMarker "-----BEGIN CERTIFICATE-----"
        $stagedTlsPublicPaths += $installedCaPath
    } elseif ($null -ne $existing -and
        $existing.ContainsKey("MAI_MEETING_SERVICE_TLS_CA_PATH")) {
        $installedCaPath = $existing["MAI_MEETING_SERVICE_TLS_CA_PATH"]
    }

    if ($effectiveTlsMode -eq "mutual") {
        if (-not [string]::IsNullOrWhiteSpace($TlsClientCertPath)) {
            $installedCertPath = Install-MeetingAiTlsPublicFile `
                -SourcePath $TlsClientCertPath `
                -DestinationPath (Join-Path $tlsRoot `
                    ("meeting-service-client-{0}.pem" -f $tlsMaterialVersion)) `
                -ExpectedMarker "-----BEGIN CERTIFICATE-----"
            $stagedTlsPublicPaths += $installedCertPath
        } elseif ($null -ne $existing -and
            $existing.ContainsKey("MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH")) {
            $installedCertPath = $existing["MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH"]
        }

        if (-not [string]::IsNullOrWhiteSpace($TlsClientKeyPath)) {
            $sourceKey = Resolve-FixedLocalPath -Path $TlsClientKeyPath `
                -Purpose "TLS client private key"
            if (-not (Test-Path -LiteralPath $sourceKey -PathType Leaf)) {
                throw "TLS client private key does not exist."
            }
            if ((Get-Item -LiteralPath $sourceKey -Force).Length -gt 262144) {
                throw "TLS client private key exceeds the 256 KiB limit."
            }
            $plainClientKey = [IO.File]::ReadAllText(
                $sourceKey,
                (New-Object Text.UTF8Encoding($false, $true))
            )
            try {
                if (-not $plainClientKey.Contains("-----BEGIN") -or
                    -not $plainClientKey.Contains("PRIVATE KEY-----")) {
                    throw "TLS client private key has an unexpected PEM type."
                }
                $clientKeyBlob = Protect-MeetingAiSecret -PlainText $plainClientKey
            } finally {
                $plainClientKey = $null
            }
        } elseif ($null -ne $existing -and
            $existing.ContainsKey("MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI")) {
            $clientKeyBlob = $existing["MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"]
        }

        if ([string]::IsNullOrWhiteSpace($installedCaPath) -or
            [string]::IsNullOrWhiteSpace($installedCertPath) -or
            [string]::IsNullOrWhiteSpace($clientKeyBlob)) {
            throw "Mutual TLS provisioning requires CA, client certificate, and private key."
        }
    }

    $clientSecretBlob = ""
    if ($null -ne $ClientSecret) {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ClientSecret)
        try {
            $plainClientSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            $clientSecretBlob = Protect-MeetingAiSecret -PlainText $plainClientSecret
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            $plainClientSecret = $null
        }
    } elseif ($null -ne $existing -and
        $existing.ContainsKey("MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI")) {
        $clientSecretBlob = $existing["MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"]
    } else {
        $prompted = Read-Host "meeting-ai OAuth client secret" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($prompted)
        try {
            $plainClientSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            $clientSecretBlob = Protect-MeetingAiSecret -PlainText $plainClientSecret
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            $plainClientSecret = $null
            $prompted.Dispose()
        }
    }

    $effectiveReadyEnabled = if (-not [string]::IsNullOrWhiteSpace(
            $ReadyConsumerEnabled
        )) {
        $ReadyConsumerEnabled.ToLowerInvariant()
    } elseif ($null -ne $existing -and
        $existing.ContainsKey("MAI_READY_CONSUMER_ENABLED")) {
        $existing["MAI_READY_CONSUMER_ENABLED"].ToLowerInvariant()
    } else {
        "false"
    }
    $permitToRevoke = ""
    $receiptToRevoke = ""
    if ($effectiveReadyEnabled -eq "false" -and $null -ne $existing -and
        $existing.ContainsKey("MAI_READY_PRE_ENABLE_PERMIT_PATH")) {
        $permitToRevoke = Assert-MeetingAiRuntimePath `
            -Path $existing["MAI_READY_PRE_ENABLE_PERMIT_PATH"] `
            -Purpose "Transcript-ready pre-enable permit"
        if ($existing.ContainsKey("MAI_READY_ACTIVATION_RECEIPT_PATH")) {
            $receiptToRevoke = Assert-MeetingAiRuntimePath `
                -Path $existing["MAI_READY_ACTIVATION_RECEIPT_PATH"] `
                -Purpose "Transcript-ready activation receipt"
        }
    }
    $transcriptSecretBlob = if ($null -ne $TranscriptServiceClientSecret) {
        Protect-SuppliedSecureValue -Value $TranscriptServiceClientSecret
    } elseif ($null -ne $existing -and
        $existing.ContainsKey("MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI")) {
        $existing["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"]
    } else {
        $prompted = Read-Host `
            "transcript-service delivery capability OAuth client secret" -AsSecureString
        try {
            Protect-SuppliedSecureValue -Value $prompted
        } finally {
            $prompted.Dispose()
        }
    }
    $readyConfig = [ordered]@{
        "MAI_READY_CONSUMER_ENABLED" = $effectiveReadyEnabled
    }
    $optionalTranscriptConfig = [ordered]@{
        "MAI_TRANSCRIPT_SERVICE_BASE_URL" = (
            Get-ExistingValue -Existing $existing `
                -Name "MAI_TRANSCRIPT_SERVICE_BASE_URL" `
                -Supplied $TranscriptServiceBaseUrl
        )
        "MAI_TRANSCRIPT_SERVICE_CAPABILITY_PATH_TEMPLATE" = (
            Get-ExistingValue -Existing $existing `
                -Name "MAI_TRANSCRIPT_SERVICE_CAPABILITY_PATH_TEMPLATE" `
                -Supplied $TranscriptServiceCapabilityPathTemplate
        )
        "MAI_TRANSCRIPT_SERVICE_TOKEN_URL" = (
            Get-ExistingValue -Existing $existing `
                -Name "MAI_TRANSCRIPT_SERVICE_TOKEN_URL" `
                -Supplied $TranscriptServiceTokenUrl
        )
        "MAI_TRANSCRIPT_SERVICE_CLIENT_ID" = (Get-ReadyConfiguredValue `
            -Existing $existing -Name "MAI_TRANSCRIPT_SERVICE_CLIENT_ID" `
            -Supplied $TranscriptServiceClientId -InitialDefault "meeting-ai")
        "MAI_TRANSCRIPT_SERVICE_AUDIENCE" = "transcript-service"
        "MAI_TRANSCRIPT_SERVICE_CAPABILITY_SCOPE" = (
            "transcript:analysis-job-capability:issue"
        )
    }
    foreach ($name in $optionalTranscriptConfig.Keys) {
        $readyConfig[$name] = $optionalTranscriptConfig[$name]
    }
    $readyConfig["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"] = $transcriptSecretBlob
    if ($effectiveReadyEnabled -eq "true") {
        if ([string]::IsNullOrWhiteSpace($effectiveAppEnv)) {
            throw "Ready consumer provisioning requires RuntimeAppEnv."
        }
        $readyRedisBlob = if ($null -ne $ReadyRedisUrl) {
            Protect-SuppliedSecureValue -Value $ReadyRedisUrl
        } elseif ($null -ne $existing -and
            $existing.ContainsKey("MAI_READY_REDIS_URL_DPAPI")) {
            $existing["MAI_READY_REDIS_URL_DPAPI"]
        } else {
            throw "Ready consumer provisioning requires ReadyRedisUrl."
        }
        if ([string]::IsNullOrWhiteSpace($ReadyPermitSourcePath) -or
            [string]::IsNullOrWhiteSpace($ReadyPermitTrustRootSourcePath)) {
            throw "Every enabled ready-consumer config write requires a fresh signed permit and trust root."
        }
        Assert-MeetingAiReadyRedisEndpoint `
            -ProtectedRedisUrl $readyRedisBlob `
            -PythonExe $PythonExe
        if ($null -ne $existing -and
            $existing.ContainsKey("MAI_READY_PRE_ENABLE_PERMIT_PATH")) {
            $previousPermitPath = Assert-MeetingAiRuntimePath `
                -Path $existing["MAI_READY_PRE_ENABLE_PERMIT_PATH"] `
                -Purpose "Previous transcript-ready pre-enable permit"
            if ($existing.ContainsKey("MAI_READY_ACTIVATION_RECEIPT_PATH")) {
                $previousActivationReceiptPath = Assert-MeetingAiRuntimePath `
                    -Path $existing["MAI_READY_ACTIVATION_RECEIPT_PATH"] `
                    -Purpose "Previous transcript-ready activation receipt"
            }
        }
        $effectiveExpectedGitopsCommit = Get-SuppliedOrExistingValue `
            -Existing $existing -Name "MAI_READY_EXPECTED_GITOPS_COMMIT" `
            -Supplied $ExpectedGitopsCommit
        $effectiveExpectedPolicySha256 = Get-SuppliedOrExistingValue `
            -Existing $existing -Name "MAI_READY_EXPECTED_POLICY_SHA256" `
            -Supplied $ExpectedPolicySha256
        $effectiveExpectedProducerDigest = Get-SuppliedOrExistingValue `
            -Existing $existing -Name "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST" `
            -Supplied $ExpectedProducerImageDigest
        $effectiveExpectedTrustRootSha256 = Get-SuppliedOrExistingValue `
            -Existing $existing `
            -Name "MAI_READY_EXPECTED_PERMIT_TRUST_ROOT_SHA256" `
            -Supplied $ExpectedPermitTrustRootSha256
        $repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
        $startupScript = Join-Path $scriptDir "start-meeting-ai.ps1"
        $activation = New-TranscriptReadyActivation `
            -PermitSourcePath $ReadyPermitSourcePath `
            -PermitTrustRootSourcePath $ReadyPermitTrustRootSourcePath `
            -TargetAppEnv $effectiveAppEnv.ToLowerInvariant() `
            -ExpectedGitopsCommit $effectiveExpectedGitopsCommit `
            -ExpectedPolicySha256 $effectiveExpectedPolicySha256 `
            -ExpectedProducerImageDigest $effectiveExpectedProducerDigest `
            -ExpectedTrustRootSha256 $effectiveExpectedTrustRootSha256 `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startupScript `
            -PythonExe $PythonExe
        $stagedPermitPath = $activation.PermitPath
        $stagedPermitTrustRootPath = $activation.TrustRootPath
        $stagedActivationReceiptPath = $activation.ReceiptPath

        $replayHorizon = if ($ReadyProducerReplayHorizonSec -gt 0) {
            $ReadyProducerReplayHorizonSec.ToString(
                "0.################",
                [Globalization.CultureInfo]::InvariantCulture
            )
        } else {
            Get-SuppliedOrExistingValue -Existing $existing `
                -Name "MAI_READY_PRODUCER_REPLAY_HORIZON_SEC"
        }
        $readyConfig["MAI_ANALYSIS_SPEC_VERSION"] = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_ANALYSIS_SPEC_VERSION" `
                -Supplied $AnalysisSpecVersion -InitialDefault "meeting-intelligence-v1")
        $readyConfig["MAI_READY_REDIS_URL_DPAPI"] = $readyRedisBlob
        $readyConfig["MAI_READY_REDIS_STREAM"] = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_READY_REDIS_STREAM" `
                -Supplied $ReadyRedisStream -InitialDefault "meeting:events")
        $readyConfig["MAI_READY_REDIS_GROUP"] = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_READY_REDIS_GROUP" `
                -Supplied $ReadyRedisGroup `
                -InitialDefault "meeting-ai-transcript-ready-v1")
        $readyConfig["MAI_READY_PRODUCER_REPLAY_HORIZON_SEC"] = $replayHorizon
        $readyConfig["MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE"] = (
                Get-SuppliedOrExistingValue -Existing $existing `
                    -Name "MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE" `
                    -Supplied $TranscriptServiceSnapshotPathTemplate
            )
        $readyConfig["MAI_TRANSCRIPT_SERVICE_SCOPE"] = "transcript:canonical:read"
        $readyConfig["MAI_READY_PRE_ENABLE_PERMIT_PATH"] = $stagedPermitPath
        $readyConfig["MAI_READY_PERMIT_TRUST_ROOT_PATH"] = `
            $stagedPermitTrustRootPath
        $readyConfig["MAI_READY_ACTIVATION_RECEIPT_PATH"] = $stagedActivationReceiptPath
        $readyConfig["MAI_READY_EXPECTED_GITOPS_COMMIT"] = $effectiveExpectedGitopsCommit
        $readyConfig["MAI_READY_EXPECTED_POLICY_SHA256"] = $effectiveExpectedPolicySha256
        $readyConfig["MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST"] = `
            $effectiveExpectedProducerDigest
        $readyConfig["MAI_READY_EXPECTED_PERMIT_TRUST_ROOT_SHA256"] = `
            $effectiveExpectedTrustRootSha256
    }

    $keyring = [ordered]@{}
    $activeKeyId = ""
    $lookupKeyId = ""
    if ($null -ne $existing -and
        $existing.ContainsKey("MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI")) {
        $oldKeyringJson = Unprotect-MeetingAiSecret `
            -ProtectedBase64 $existing["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
            -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
        $oldKeyring = $oldKeyringJson | ConvertFrom-Json -ErrorAction Stop
        foreach ($property in $oldKeyring.PSObject.Properties) {
            $keyring[$property.Name] = [string]$property.Value
        }
        $activeKeyId = $existing["MAI_INGESTION_ACTIVE_KEY_ID"]
        if ($existing.ContainsKey("MAI_INGESTION_LOOKUP_KEY_ID")) {
            $lookupKeyId = $existing["MAI_INGESTION_LOOKUP_KEY_ID"]
        }
    }

    if ([string]::IsNullOrWhiteSpace($lookupKeyId)) {
        $lookupBytes = New-Object byte[] 32
        $lookupRng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $lookupRng.GetBytes($lookupBytes)
            $lookupKeyId = "lookup-{0}" -f [Guid]::NewGuid().ToString("N")
            if ($keyring.Contains($lookupKeyId)) {
                throw "Generated blind-index key identifier already exists."
            }
            $keyring[$lookupKeyId] = [Convert]::ToBase64String($lookupBytes)
        } finally {
            $lookupRng.Dispose()
            [Array]::Clear($lookupBytes, 0, $lookupBytes.Length)
        }
    }

    if ([string]::IsNullOrWhiteSpace($activeKeyId) -or $RotateEncryptionKey) {
        $keyBytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $rng.GetBytes($keyBytes)
            $activeKeyId = "{0}-{1}" -f `
                (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"),
                ([Guid]::NewGuid().ToString("N").Substring(0, 8))
            if ($keyring.Contains($activeKeyId)) {
                throw "Generated encryption key identifier already exists."
            }
            $keyring[$activeKeyId] = [Convert]::ToBase64String($keyBytes)
        } finally {
            $rng.Dispose()
            [Array]::Clear($keyBytes, 0, $keyBytes.Length)
        }
    }

    $keyringJson = $keyring | ConvertTo-Json -Compress
    Assert-MeetingAiKeyring -KeyringJson $keyringJson `
        -ActiveKeyId $activeKeyId -LookupKeyId $lookupKeyId
    $keyringBlob = Protect-MeetingAiSecret -PlainText $keyringJson

    $config = [ordered]@{
        "MAI_INGESTION_ENABLED" = "true"
        "MAI_MEETING_SERVICE_BASE_URL" = $baseUrl
        "MAI_MEETING_SERVICE_TOKEN_URL" = $tokenUrl
        "MAI_MEETING_SERVICE_CLIENT_ID" = $ClientId
        "MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI" = $clientSecretBlob
        "MAI_MEETING_SERVICE_AUDIENCE" = $Audience
        "MAI_MEETING_SERVICE_SCOPE" = $Permission
        "MAI_MEETING_SERVICE_TLS_MODE" = $effectiveTlsMode
        "MAI_INGESTION_STORE_PATH" = $StorePath
        "MAI_INGESTION_ACTIVE_KEY_ID" = $activeKeyId
        "MAI_INGESTION_LOOKUP_KEY_ID" = $lookupKeyId
        "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI" = $keyringBlob
    }
    if (-not [string]::IsNullOrWhiteSpace($effectiveAppEnv)) {
        $config["MAI_APP_ENV"] = $effectiveAppEnv.ToLowerInvariant()
    }
    if (-not [string]::IsNullOrWhiteSpace($installedCaPath)) {
        $config["MAI_MEETING_SERVICE_TLS_CA_PATH"] = $installedCaPath
    }
    if ($effectiveTlsMode -eq "mutual") {
        $config["MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH"] = $installedCertPath
        $config["MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"] = $clientKeyBlob
        $config["MAI_MEETING_SERVICE_TLS_RELOAD_INTERVAL_SEC"] = "60"
    }
    foreach ($name in $readyConfig.Keys) {
        $config[$name] = $readyConfig[$name]
    }
    $content = ConvertTo-MeetingAiConfigContent -Values $config

    [void](Initialize-MeetingAiDirectory -Path (Split-Path -Parent $StorePath))
    if ($env:CI -eq "true" -and
        $env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE -eq "1") {
        throw "TEST_INJECTED_MEETING_AI_CONFIG_WRITE_FAILURE"
    }
    Write-MeetingAiConfigAtomic -Path $ConfigPath -Content $content
    $tlsPublicArtifactsCommitted = $true
    if ($effectiveReadyEnabled -eq "true") {
        $activationCommitted = $true
        if (-not [string]::IsNullOrWhiteSpace($previousPermitPath) -and
            $previousPermitPath -ne $stagedPermitPath -and
            (Test-Path -LiteralPath $previousPermitPath -PathType Leaf)) {
            Remove-Item -LiteralPath $previousPermitPath -Force
        }
        if (-not [string]::IsNullOrWhiteSpace($previousActivationReceiptPath) -and
            $previousActivationReceiptPath -ne $stagedActivationReceiptPath -and
            (Test-Path -LiteralPath $previousActivationReceiptPath -PathType Leaf)) {
            Remove-Item -LiteralPath $previousActivationReceiptPath -Force
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($permitToRevoke) -and
        (Test-Path -LiteralPath $permitToRevoke -PathType Leaf)) {
        Remove-Item -LiteralPath $permitToRevoke -Force
    }
    if (-not [string]::IsNullOrWhiteSpace($receiptToRevoke) -and
        (Test-Path -LiteralPath $receiptToRevoke -PathType Leaf)) {
        Remove-Item -LiteralPath $receiptToRevoke -Force
    }
    Write-Host "meeting-ai runtime config written: $ConfigPath"
    Write-Host "active encryption key id: $activeKeyId"
    Write-Host "Restart task with schtasks.exe /End and /Run for platform-ai-meeting-ai."
} finally {
    $activeConfigValues = $null
    $activeConfigReadUncertain = $false
    if ((-not $activationCommitted -or -not $tlsPublicArtifactsCommitted) -and
        (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        try {
            $activeConfigValues = Read-MeetingAiConfigFile -Path $ConfigPath
            Assert-MeetingAiConfigValues -Values $activeConfigValues
        } catch {
            $activeConfigReadUncertain = $true
        }
    }
    if (-not $activationCommitted) {
        foreach ($staged in @(
                [pscustomobject]@{
                    Path = $stagedPermitPath
                    ConfigKey = "MAI_READY_PRE_ENABLE_PERMIT_PATH"
                },
                [pscustomobject]@{
                    Path = $stagedActivationReceiptPath
                    ConfigKey = "MAI_READY_ACTIVATION_RECEIPT_PATH"
                }
            )) {
            $artifact = [string]$staged.Path
            $configKey = [string]$staged.ConfigKey
            $referencedByActiveConfig = $activeConfigReadUncertain -or (
                $null -ne $activeConfigValues -and
                $activeConfigValues.ContainsKey($configKey) -and
                $activeConfigValues[$configKey].Equals(
                    $artifact,
                    [StringComparison]::OrdinalIgnoreCase
                )
            )
            if (-not [string]::IsNullOrWhiteSpace($artifact) -and
                -not $referencedByActiveConfig -and
                $artifact -ne $previousPermitPath -and
                $artifact -ne $previousActivationReceiptPath -and
                (Test-Path -LiteralPath $artifact -PathType Leaf)) {
                Remove-Item -LiteralPath $artifact -Force
            }
        }
    }
    if (-not $tlsPublicArtifactsCommitted) {
        foreach ($artifact in $stagedTlsPublicPaths) {
            $referencedByActiveConfig = $activeConfigReadUncertain
            if (-not $referencedByActiveConfig -and
                $null -ne $activeConfigValues) {
                foreach ($configKey in @(
                        "MAI_MEETING_SERVICE_TLS_CA_PATH",
                        "MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH"
                    )) {
                    if ($activeConfigValues.ContainsKey($configKey) -and
                        $activeConfigValues[$configKey].Equals(
                            $artifact,
                            [StringComparison]::OrdinalIgnoreCase
                        )) {
                        $referencedByActiveConfig = $true
                        break
                    }
                }
            }
            if (-not [string]::IsNullOrWhiteSpace($artifact) -and
                -not $referencedByActiveConfig -and
                (Test-Path -LiteralPath $artifact -PathType Leaf)) {
                Remove-Item -LiteralPath $artifact -Force
            }
        }
    }
    if ($lockTaken) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
