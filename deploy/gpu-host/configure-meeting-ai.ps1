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
    [string]$ExpectedGitopsCommit = "",
    [string]$ExpectedPolicySha256 = "",
    [string]$ExpectedProducerImageDigest = "",
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
$previousPermitPath = ""
$permitActivated = $false

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

try {
    $lockTaken = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
    if (-not $lockTaken) { throw "Another meeting-ai configuration operation is in progress." }

    [void](Initialize-MeetingAiRuntimeRoot)

    if ($RestoreBackup) {
        $backupPath = "$ConfigPath.bak"
        if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            throw "No meeting-ai runtime config backup exists."
        }
        $backupValues = Read-MeetingAiConfigFile -Path $backupPath
        Assert-MeetingAiConfigValues -Values $backupValues
        $backupContent = [IO.File]::ReadAllText(
            $backupPath,
            (New-Object Text.UTF8Encoding($false, $true))
        )
        Write-MeetingAiConfigAtomic -Path $ConfigPath -Content $backupContent
        Write-Host "meeting-ai runtime config backup restored: $ConfigPath"
        Write-Host "Restart task with schtasks.exe /End and /Run for platform-ai-meeting-ai."
        return
    }

    $existing = $null
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        $existing = Read-MeetingAiConfigFile -Path $ConfigPath
        Assert-MeetingAiConfigValues -Values $existing
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
    if ($effectiveReadyEnabled -eq "false" -and $null -ne $existing -and
        $existing.ContainsKey("MAI_READY_PRE_ENABLE_PERMIT_PATH")) {
        $permitToRevoke = Assert-MeetingAiRuntimePath `
            -Path $existing["MAI_READY_PRE_ENABLE_PERMIT_PATH"] `
            -Purpose "Transcript-ready pre-enable permit"
    }
    $readyConfig = [ordered]@{
        "MAI_READY_CONSUMER_ENABLED" = $effectiveReadyEnabled
    }
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
        $transcriptSecretBlob = if ($null -ne $TranscriptServiceClientSecret) {
            Protect-SuppliedSecureValue -Value $TranscriptServiceClientSecret
        } elseif ($null -ne $existing -and
            $existing.ContainsKey("MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI")) {
            $existing["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"]
        } else {
            throw "Ready consumer provisioning requires TranscriptServiceClientSecret."
        }

        if ([string]::IsNullOrWhiteSpace($ReadyPermitSourcePath)) {
            throw "Every enabled ready-consumer config write requires a fresh permit source."
        }
        if ($null -ne $existing -and
            $existing.ContainsKey("MAI_READY_PRE_ENABLE_PERMIT_PATH")) {
            $previousPermitPath = Assert-MeetingAiRuntimePath `
                -Path $existing["MAI_READY_PRE_ENABLE_PERMIT_PATH"] `
                -Purpose "Previous transcript-ready pre-enable permit"
        }
        $permitSource = Resolve-FixedLocalPath `
            -Path $ReadyPermitSourcePath `
            -Purpose "Transcript-ready pre-enable permit source"
        if (-not (Test-Path -LiteralPath $permitSource -PathType Leaf) -or
            (Get-Item -LiteralPath $permitSource -Force).Length -gt 1048576) {
            throw "Transcript-ready pre-enable permit source is missing or too large."
        }
        $permitBytes = [IO.File]::ReadAllBytes($permitSource)
        $permitHasher = [Security.Cryptography.SHA256]::Create()
        $permitHashBytes = $null
        try {
            $permitUtf8 = New-Object Text.UTF8Encoding($false, $true)
            $permitContent = $permitUtf8.GetString($permitBytes)
            $permitHashBytes = $permitHasher.ComputeHash($permitBytes)
            $permitSha256 = ([BitConverter]::ToString($permitHashBytes)).Replace(
                "-", ""
            ).ToLowerInvariant()
        } finally {
            $permitHasher.Dispose()
            if ($null -ne $permitHashBytes) {
                [Array]::Clear($permitHashBytes, 0, $permitHashBytes.Length)
            }
            [Array]::Clear($permitBytes, 0, $permitBytes.Length)
        }
        $stagedPermitPath = Join-Path (Get-MeetingAiRuntimeRoot) `
            ("permits\transcript-ready-pre-enable-{0}.json" -f $permitSha256)
        Write-MeetingAiSecretFileAtomic -Path $stagedPermitPath -Content $permitContent
        $permitContent = $null

        $effectiveExpectedGitopsCommit = Get-SuppliedOrExistingValue `
            -Existing $existing -Name "MAI_READY_EXPECTED_GITOPS_COMMIT" `
            -Supplied $ExpectedGitopsCommit
        $effectiveExpectedPolicySha256 = Get-SuppliedOrExistingValue `
            -Existing $existing -Name "MAI_READY_EXPECTED_POLICY_SHA256" `
            -Supplied $ExpectedPolicySha256
        $effectiveExpectedProducerDigest = Get-SuppliedOrExistingValue `
            -Existing $existing -Name "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST" `
            -Supplied $ExpectedProducerImageDigest
        $repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
        $startupScript = Join-Path $scriptDir "start-meeting-ai.ps1"
        [void](Assert-TranscriptReadyPermitFile `
            -PermitPath $stagedPermitPath `
            -ExpectedGitopsCommit $effectiveExpectedGitopsCommit `
            -ExpectedPolicySha256 $effectiveExpectedPolicySha256 `
            -ExpectedProducerImageDigest $effectiveExpectedProducerDigest `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startupScript `
            -AppEnv $effectiveAppEnv.ToLowerInvariant())

        $replayHorizon = if ($ReadyProducerReplayHorizonSec -gt 0) {
            $ReadyProducerReplayHorizonSec.ToString(
                "0.################",
                [Globalization.CultureInfo]::InvariantCulture
            )
        } else {
            Get-SuppliedOrExistingValue -Existing $existing `
                -Name "MAI_READY_PRODUCER_REPLAY_HORIZON_SEC"
        }
        $readyConfig = [ordered]@{
            "MAI_READY_CONSUMER_ENABLED" = "true"
            "MAI_ANALYSIS_SPEC_VERSION" = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_ANALYSIS_SPEC_VERSION" `
                -Supplied $AnalysisSpecVersion `
                -InitialDefault "meeting-intelligence-v1")
            "MAI_READY_REDIS_URL_DPAPI" = $readyRedisBlob
            "MAI_READY_REDIS_STREAM" = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_READY_REDIS_STREAM" `
                -Supplied $ReadyRedisStream -InitialDefault "meeting:events")
            "MAI_READY_REDIS_GROUP" = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_READY_REDIS_GROUP" `
                -Supplied $ReadyRedisGroup `
                -InitialDefault "meeting-ai-transcript-ready-v1")
            "MAI_READY_PRODUCER_REPLAY_HORIZON_SEC" = $replayHorizon
            "MAI_TRANSCRIPT_SERVICE_BASE_URL" = (
                Get-SuppliedOrExistingValue -Existing $existing `
                    -Name "MAI_TRANSCRIPT_SERVICE_BASE_URL" `
                    -Supplied $TranscriptServiceBaseUrl
            )
            "MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE" = (
                Get-SuppliedOrExistingValue -Existing $existing `
                    -Name "MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE" `
                    -Supplied $TranscriptServiceSnapshotPathTemplate
            )
            "MAI_TRANSCRIPT_SERVICE_CAPABILITY_PATH_TEMPLATE" = (
                Get-SuppliedOrExistingValue -Existing $existing `
                    -Name "MAI_TRANSCRIPT_SERVICE_CAPABILITY_PATH_TEMPLATE" `
                    -Supplied $TranscriptServiceCapabilityPathTemplate
            )
            "MAI_TRANSCRIPT_SERVICE_TOKEN_URL" = (
                Get-SuppliedOrExistingValue -Existing $existing `
                    -Name "MAI_TRANSCRIPT_SERVICE_TOKEN_URL" `
                    -Supplied $TranscriptServiceTokenUrl
            )
            "MAI_TRANSCRIPT_SERVICE_CLIENT_ID" = (Get-ReadyConfiguredValue `
                -Existing $existing -Name "MAI_TRANSCRIPT_SERVICE_CLIENT_ID" `
                -Supplied $TranscriptServiceClientId -InitialDefault "meeting-ai")
            "MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI" = $transcriptSecretBlob
            "MAI_TRANSCRIPT_SERVICE_AUDIENCE" = "transcript-service"
            "MAI_TRANSCRIPT_SERVICE_SCOPE" = "transcript:canonical:read"
            "MAI_TRANSCRIPT_SERVICE_CAPABILITY_SCOPE" = (
                "transcript:analysis-job-capability:issue"
            )
            "MAI_READY_PRE_ENABLE_PERMIT_PATH" = $stagedPermitPath
            "MAI_READY_EXPECTED_GITOPS_COMMIT" = $effectiveExpectedGitopsCommit
            "MAI_READY_EXPECTED_POLICY_SHA256" = $effectiveExpectedPolicySha256
            "MAI_READY_EXPECTED_PRODUCER_IMAGE_DIGEST" = `
                $effectiveExpectedProducerDigest
        }
    }

    $keyring = [ordered]@{}
    $activeKeyId = ""
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
        Assert-MeetingAiKeyring -KeyringJson $oldKeyringJson -ActiveKeyId $activeKeyId
    }

    if ($keyring.Count -eq 0 -or $RotateEncryptionKey) {
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
    Assert-MeetingAiKeyring -KeyringJson $keyringJson -ActiveKeyId $activeKeyId
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
    $lines = @(
        "# platform-ai meeting-ai runtime config v1"
        "# Secret fields are DPAPI LocalMachine ciphertext. Do not copy to another host."
    )
    foreach ($name in $config.Keys) {
        $lines += "{0}={1}" -f $name, $config[$name]
    }
    $content = ($lines -join "`r`n") + "`r`n"

    [void](Initialize-MeetingAiDirectory -Path (Split-Path -Parent $StorePath))
    if ($env:CI -eq "true" -and
        $env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE -eq "1") {
        throw "TEST_INJECTED_MEETING_AI_CONFIG_WRITE_FAILURE"
    }
    Write-MeetingAiConfigAtomic -Path $ConfigPath -Content $content
    if ($effectiveReadyEnabled -eq "true") {
        $permitActivated = $true
        if (-not [string]::IsNullOrWhiteSpace($previousPermitPath) -and
            $previousPermitPath -ne $stagedPermitPath -and
            (Test-Path -LiteralPath $previousPermitPath -PathType Leaf)) {
            Remove-Item -LiteralPath $previousPermitPath -Force
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($permitToRevoke) -and
        (Test-Path -LiteralPath $permitToRevoke -PathType Leaf)) {
        Remove-Item -LiteralPath $permitToRevoke -Force
    }
    Write-Host "meeting-ai runtime config written: $ConfigPath"
    Write-Host "active encryption key id: $activeKeyId"
    Write-Host "Restart task with schtasks.exe /End and /Run for platform-ai-meeting-ai."
} finally {
    if (-not $permitActivated -and
        -not [string]::IsNullOrWhiteSpace($stagedPermitPath) -and
        $stagedPermitPath -ne $previousPermitPath -and
        (Test-Path -LiteralPath $stagedPermitPath -PathType Leaf)) {
        Remove-Item -LiteralPath $stagedPermitPath -Force
    }
    if ($lockTaken) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
