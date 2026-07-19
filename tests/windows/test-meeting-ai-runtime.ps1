# Windows PowerShell 5.1 behavior contract for the meeting-ai runtime config.
# Runs only on an ephemeral windows-latest GitHub runner. No value is printed.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$deployDir = Join-Path $repoRoot "deploy\gpu-host"
$runtimeScript = Join-Path $deployDir "meeting-ai-runtime-env.ps1"
$configureScript = Join-Path $deployDir "configure-meeting-ai.ps1"
$startScript = Join-Path $deployDir "start-meeting-ai.ps1"
. $runtimeScript

$runtimeRoot = Get-MeetingAiRuntimeRoot
$configPath = Join-Path $runtimeRoot "meeting-ai.env"
$storePath = Join-Path $runtimeRoot "meeting-ai\analysis-delivery.sqlite3"
$plainTestCredential = "ci-ephemeral-credential"
$secureCredential = ConvertTo-SecureString $plainTestCredential -AsPlainText -Force
$tlsSourceRoot = Join-Path $env:RUNNER_TEMP "meeting-ai-mtls-source"
$tlsCaSource = Join-Path $tlsSourceRoot "ca.pem"
$tlsCertSource = Join-Path $tlsSourceRoot "client.pem"
$tlsKeySource = Join-Path $tlsSourceRoot "client.key"
$startupProbeRoot = Join-Path $env:RUNNER_TEMP "meeting-ai-startup-cleanup"
$plainTestKey = "-----BEGIN PRIVATE KEY-----`nci-ephemeral-key`n-----END PRIVATE KEY-----"
$plainReadyRedisUrl = "redis://ci-user:ci-password@127.0.0.1:6379/0"
$secureReadyRedisUrl = ConvertTo-SecureString $plainReadyRedisUrl -AsPlainText -Force
$plainTranscriptCredential = "ci-transcript-credential"
$secureTranscriptCredential = ConvertTo-SecureString `
    $plainTranscriptCredential -AsPlainText -Force
$permitSource = Join-Path $env:RUNNER_TEMP "transcript-ready-pre-enable.json"
$expectedGitopsCommit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
$expectedPolicySha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
$expectedProducerDigest = "sha256:" + (("c" * 64) -join "")

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-ThrowsLike {
    param([scriptblock]$Action, [string]$Expected)
    try {
        & $Action
    } catch {
        if ($_.Exception.Message -notlike "*$Expected*") {
            throw ("Expected error containing '{0}', got {1}: {2}" -f `
                $Expected,
                $_.Exception.GetType().Name,
                $_.Exception.Message)
        }
        return
    }
    throw "Expected an exception containing '$Expected'."
}

try {
    if (Test-Path -LiteralPath $runtimeRoot) {
        Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
    }

    & $configureScript `
        -MeetingServiceBaseUrl "https://meeting.internal.example" `
        -MeetingServiceTokenUrl "https://auth.internal.example/oauth2/token" `
        -ClientId "meeting-ai-ci" `
        -ClientSecret $secureCredential `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -WhatIf
    Assert-True (-not (Test-Path -LiteralPath $runtimeRoot)) `
        "WhatIf must not create directories, ACLs, keys, or config files."

    & $configureScript `
        -MeetingServiceBaseUrl "https://meeting.internal.example" `
        -MeetingServiceTokenUrl "https://auth.internal.example/oauth2/token" `
        -ClientId "meeting-ai-ci" `
        -ClientSecret $secureCredential `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false

    Assert-MeetingAiAcl -Path (Join-Path $env:ProgramData "Acik") -Directory
    Assert-MeetingAiAcl -Path $runtimeRoot -Directory
    Assert-MeetingAiAcl -Path $configPath
    Assert-MeetingAiAcl -Path (Split-Path -Parent $storePath) -Directory

    $configText = [IO.File]::ReadAllText($configPath)
    Assert-True (-not $configText.Contains($plainTestCredential)) `
        "Plain credential must not be stored in the runtime config."
    Assert-True ($configText.Contains("MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI=")) `
        "DPAPI client credential blob is missing."
    Assert-True (-not $configText.Contains("MAI_MEETING_SERVICE_CLIENT_SECRET=")) `
        "Plain client credential key must not be stored."
    Assert-True ($configText.Contains("MAI_READY_CONSUMER_ENABLED=false")) `
        "Ready consumer must be explicitly default-off."

    $loaded = Import-MeetingAiRuntimeEnvironment -Path $configPath
    Assert-True $loaded "Runtime config import must succeed."
    Assert-True ($env:MAI_MEETING_SERVICE_CLIENT_SECRET -eq $plainTestCredential) `
        "DPAPI client credential round trip failed."
    Assert-True ($env:MAI_MEETING_SERVICE_CLIENT_ID -eq "meeting-ai-ci") `
        "Non-secret runtime value import failed."
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI
    )) "DPAPI blob must not be exported to the child environment."

    New-Item -ItemType Directory -Path $tlsSourceRoot -Force | Out-Null
    [IO.File]::WriteAllText(
        $tlsCaSource,
        "-----BEGIN CERTIFICATE-----`nci-ca`n-----END CERTIFICATE-----",
        (New-Object Text.UTF8Encoding($false))
    )
    [IO.File]::WriteAllText(
        $tlsCertSource,
        "-----BEGIN CERTIFICATE-----`nci-client`n-----END CERTIFICATE-----",
        (New-Object Text.UTF8Encoding($false))
    )
    [IO.File]::WriteAllText(
        $tlsKeySource,
        $plainTestKey,
        (New-Object Text.UTF8Encoding($false))
    )
    & $configureScript `
        -TlsMode "mutual" `
        -TlsCaPath $tlsCaSource `
        -TlsClientCertPath $tlsCertSource `
        -TlsClientKeyPath $tlsKeySource `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $mtlsConfigText = [IO.File]::ReadAllText($configPath)
    Assert-True ($mtlsConfigText.Contains("MAI_MEETING_SERVICE_TLS_MODE=mutual")) `
        "Mutual TLS mode is missing."
    Assert-True ($mtlsConfigText.Contains("MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI=")) `
        "DPAPI client key blob is missing."
    Assert-True (-not $mtlsConfigText.Contains($plainTestKey)) `
        "Plain client key must not be stored in config."

    Assert-True (Import-MeetingAiRuntimeEnvironment -Path $configPath) `
        "Mutual TLS runtime config import must succeed."
    $runtimeKeyPath = Get-MeetingAiRuntimeTlsKeyPath
    Assert-True (Test-Path -LiteralPath $runtimeKeyPath -PathType Leaf) `
        "Runtime client key was not materialized."
    Assert-MeetingAiAcl -Path $runtimeKeyPath
    Assert-True ($env:MAI_MEETING_SERVICE_TLS_CLIENT_KEY_PATH -eq $runtimeKeyPath) `
        "Runtime client key path was not exported."
    Assert-True ([IO.File]::ReadAllText($runtimeKeyPath).Contains("PRIVATE KEY")) `
        "Runtime client key materialization failed."
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI
    )) "DPAPI key blob must not be exported to the child environment."

    Assert-True (Import-MeetingAiRuntimeEnvironment -Path $configPath) `
        "Mutual TLS runtime config import must be idempotent before launcher startup."

    $repoCommit = (& git -C $repoRoot rev-parse HEAD).Trim().ToLowerInvariant()
    Assert-True ($LASTEXITCODE -eq 0) "Test repository commit could not be read."
    $startupSha256 = Get-MeetingAiFileSha256 `
        -Path $startScript -Purpose "Meeting-ai startup script"
    $permit = [ordered]@{
        schemaVersion = "faz24.transcriptReadyPreEnableVerdict.v1"
        generatedAt = [DateTimeOffset]::UtcNow.ToString("o")
        issue = "platform-k8s-gitops#2610"
        status = "accepted-candidate"
        enableAuthorized = $true
        checks = @([ordered]@{ name = "ci-contract"; passed = $true; message = "ok" })
        requiredRemediationEvidence = @()
        binding = [ordered]@{
            expectedGitopsCommit = $expectedGitopsCommit
            policySha256 = $expectedPolicySha256
            producerCapability = [ordered]@{
                transcriptImageDigest = $expectedProducerDigest
            }
            hostStartupGuard = [ordered]@{
                platformAiCommit = $repoCommit
                startupScriptSha256 = $startupSha256
                permitRequired = $true
            }
        }
    }
    [IO.File]::WriteAllText(
        $permitSource,
        (($permit | ConvertTo-Json -Depth 8) + "`n"),
        (New-Object Text.UTF8Encoding($false))
    )

    & $configureScript `
        -ReadyConsumerEnabled "true" `
        -RuntimeAppEnv "test" `
        -ReadyRedisUrl $secureReadyRedisUrl `
        -ReadyProducerReplayHorizonSec 2592000 `
        -TranscriptServiceBaseUrl "https://transcript.internal.example" `
        -TranscriptServiceSnapshotPathTemplate "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}" `
        -TranscriptServiceCapabilityPathTemplate "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}/analysis-capability" `
        -TranscriptServiceTokenUrl "https://auth.internal.example/oauth2/token" `
        -TranscriptServiceClientSecret $secureTranscriptCredential `
        -ReadyPermitSourcePath $permitSource `
        -ExpectedGitopsCommit $expectedGitopsCommit `
        -ExpectedPolicySha256 $expectedPolicySha256 `
        -ExpectedProducerImageDigest $expectedProducerDigest `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false

    $readyConfigText = [IO.File]::ReadAllText($configPath)
    Assert-True ($readyConfigText.Contains("MAI_READY_CONSUMER_ENABLED=true")) `
        "Ready consumer enable flag is missing."
    Assert-True ($readyConfigText.Contains("MAI_READY_REDIS_URL_DPAPI=")) `
        "Ready Redis DPAPI blob is missing."
    Assert-True (-not $readyConfigText.Contains($plainReadyRedisUrl)) `
        "Plain Redis URL must not be stored."
    Assert-True ($readyConfigText.Contains("MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI=")) `
        "Transcript-service DPAPI credential blob is missing."
    Assert-True (-not $readyConfigText.Contains($plainTranscriptCredential)) `
        "Plain transcript-service credential must not be stored."

    Assert-True (Import-MeetingAiRuntimeEnvironment -Path $configPath) `
        "Ready consumer runtime config import must succeed."
    Assert-True ($env:MAI_READY_REDIS_URL -eq $plainReadyRedisUrl) `
        "Ready Redis DPAPI round trip failed."
    Assert-True ($env:MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET -eq $plainTranscriptCredential) `
        "Transcript-service DPAPI round trip failed."
    Assert-True ([string]::IsNullOrWhiteSpace($env:MAI_READY_REDIS_URL_DPAPI)) `
        "Ready Redis DPAPI blob must not be exported."
    Assert-TranscriptReadyPreEnablePermit `
        -RepoRoot $repoRoot `
        -StartupScriptPath $startScript

    $installedPermit = $env:MAI_READY_PRE_ENABLE_PERMIT_PATH
    $stalePermit = (($permit | ConvertTo-Json -Depth 8) | ConvertFrom-Json)
    $stalePermit.generatedAt = [DateTimeOffset]::UtcNow.AddMinutes(-20).ToString("o")
    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit `
        -Content (($stalePermit | ConvertTo-Json -Depth 8) + "`n")
    Assert-ThrowsLike {
        Assert-TranscriptReadyPreEnablePermit `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startScript
    } "outside the 900 second startup window"
    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit `
        -Content (($permit | ConvertTo-Json -Depth 8) + "`n")

    Assert-ThrowsLike {
        & $startScript `
            -RepoRoot $startupProbeRoot `
            -Backend "mock" `
            -RuntimeConfigPath $configPath `
            -PythonExe "not-invoked.exe"
    } "mock meeting-ai backend is forbidden"
    Assert-True (-not (Test-Path -LiteralPath $runtimeKeyPath -PathType Leaf)) `
        "Startup rejection must clean the materialized plaintext client key."
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_MEETING_SERVICE_TLS_CLIENT_KEY_PATH
    )) "Startup rejection must clear the runtime key environment path."

    $beforeValues = Read-MeetingAiConfigFile -Path $configPath
    $beforeActive = $beforeValues["MAI_INGESTION_ACTIVE_KEY_ID"]
    $beforeJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $beforeValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    $beforeCount = @(($beforeJson | ConvertFrom-Json).PSObject.Properties).Count

    & $configureScript `
        -RotateEncryptionKey `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $rotatedValues = Read-MeetingAiConfigFile -Path $configPath
    $rotatedJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $rotatedValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    $rotatedCount = @(($rotatedJson | ConvertFrom-Json).PSObject.Properties).Count
    Assert-True ($rotatedCount -eq ($beforeCount + 1)) `
        "Rotation must append exactly one key."
    Assert-True ($rotatedValues["MAI_INGESTION_ACTIVE_KEY_ID"] -ne $beforeActive) `
        "Rotation must select a new active key."
    Assert-True (Test-Path -LiteralPath "$configPath.bak") `
        "Atomic rotation backup is missing."
    Assert-MeetingAiAcl -Path "$configPath.bak"

    & $configureScript `
        -RestoreBackup `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $restoredValues = Read-MeetingAiConfigFile -Path $configPath
    Assert-True ($restoredValues["MAI_INGESTION_ACTIVE_KEY_ID"] -eq $beforeActive) `
        "Backup restore did not restore the previous active key."

    $acl = Get-Acl -LiteralPath $configPath
    $users = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-545")
    $broadRule = New-Object Security.AccessControl.FileSystemAccessRule(
        $users,
        [Security.AccessControl.FileSystemRights]::Read,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($broadRule)
    Set-Acl -LiteralPath $configPath -AclObject $acl
    Assert-ThrowsLike {
        Import-MeetingAiRuntimeEnvironment -Path $configPath
    } "outside the service allowlist"
    Set-Acl -LiteralPath $configPath -AclObject (New-MeetingAiAcl)

    $missing = Join-Path $runtimeRoot "missing.env"
    Assert-True (-not (Import-MeetingAiRuntimeEnvironment -Path $missing -Optional)) `
        "Optional missing config must return false."

    $duplicate = Join-Path $runtimeRoot "duplicate.env"
    $duplicateText = $configText + "MAI_INGESTION_ENABLED=true`r`n"
    [IO.File]::WriteAllText($duplicate, $duplicateText, (New-Object Text.UTF8Encoding($false)))
    Set-Acl -LiteralPath $duplicate -AclObject (New-MeetingAiAcl)
    Assert-ThrowsLike { Read-MeetingAiConfigFile -Path $duplicate } "duplicate key"

    $unknown = Join-Path $runtimeRoot "unknown.env"
    $unknownText = $configText + "MAI_UNKNOWN_VALUE=blocked`r`n"
    [IO.File]::WriteAllText($unknown, $unknownText, (New-Object Text.UTF8Encoding($false)))
    Set-Acl -LiteralPath $unknown -AclObject (New-MeetingAiAcl)
    Assert-ThrowsLike { Read-MeetingAiConfigFile -Path $unknown } "unknown key"

    Write-Host "meeting-ai Windows runtime contract: PASS"
} finally {
    Clear-MeetingAiRuntimeTlsKey
    $env:MAI_MEETING_SERVICE_CLIENT_SECRET = $null
    $env:MAI_INGESTION_ENCRYPTION_KEYS_JSON = $null
    $env:MAI_READY_REDIS_URL = $null
    $env:MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET = $null
    $secureCredential.Dispose()
    $secureReadyRedisUrl.Dispose()
    $secureTranscriptCredential.Dispose()
    if (Test-Path -LiteralPath $runtimeRoot) {
        Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $tlsSourceRoot) {
        Remove-Item -LiteralPath $tlsSourceRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $startupProbeRoot) {
        Remove-Item -LiteralPath $startupProbeRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $permitSource) {
        Remove-Item -LiteralPath $permitSource -Force
    }
}
