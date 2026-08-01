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
$permitSource = Join-Path $env:RUNNER_TEMP "transcript-ready-pre-enable.dsse.json"
$permitTrustRootSource = Join-Path $env:RUNNER_TEMP "transcript-ready-trust-root.json"
$permitPrivateKeyPath = Join-Path $env:RUNNER_TEMP "transcript-ready-test-key.raw"
$permitFixtureScript = Join-Path $PSScriptRoot `
    "create-transcript-ready-permit-fixture.py"
$pythonExe = (Get-Command python -CommandType Application -ErrorAction Stop | `
    Select-Object -First 1).Source
$expectedPermitTrustRootSha256 = ""
$expectedGitopsCommit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
$expectedPolicySha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
$expectedProducerDigest = "sha256:" + (("c" * 64) -join "")
$permitSequenceOffsetSeconds = 0
$customReadyStream = "meeting:events:ci-custom"
$customReadyGroup = "meeting-ai-ready-ci-custom"
$customAnalysisSpec = "meeting-intelligence-ci-custom"
$customTranscriptClientId = "meeting-ai-ci-custom"
$previousCi = $env:CI
$env:CI = "true"

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

function Assert-PythonRuntimeConfigLoads {
    Push-Location (Join-Path $repoRoot "services\meeting-ai-service")
    try {
        & $pythonExe -c "from app.core.config import Settings; Settings()"
        if ($LASTEXITCODE -ne 0) {
            throw "Python runtime settings rejected the imported meeting-ai config."
        }
    } finally {
        Pop-Location
    }
}

function Copy-ConfigValues {
    param($Values)

    $copy = @{}
    foreach ($name in $Values.Keys) { $copy[$name] = $Values[$name] }
    return $copy
}

function ConvertTo-TestMeetingAiConfigContent {
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

function Write-FreshPermit {
    param(
        [string]$Path = $permitSource,
        [ValidateSet("test", "stage")][string]$AppEnv = "test",
        [int]$GeneratedAtOffsetSeconds = 0
    )
    $effectiveGeneratedAtOffsetSeconds = $GeneratedAtOffsetSeconds
    if (-not $PSBoundParameters.ContainsKey("GeneratedAtOffsetSeconds")) {
        $effectiveGeneratedAtOffsetSeconds = $script:permitSequenceOffsetSeconds
        $script:permitSequenceOffsetSeconds += 1
    }
    $fixtureArgs = @(
        $permitFixtureScript,
        "--envelope", $Path,
        "--trust-root", $permitTrustRootSource,
        "--private-key", $permitPrivateKeyPath,
        "--app-env", $AppEnv,
        "--gitops-commit", $expectedGitopsCommit,
        "--policy-sha256", $expectedPolicySha256,
        "--producer-digest", $expectedProducerDigest,
        "--backend-commit", (("f" * 40) -join ""),
        "--platform-ai-commit", $repoCommit,
        "--startup-sha256", $startupSha256,
        "--generated-at-offset-seconds", "$effectiveGeneratedAtOffsetSeconds"
    )
    $trustSha = @(& $pythonExe @fixtureArgs)
    if ($LASTEXITCODE -ne 0 -or $trustSha.Count -ne 1 -or
        "$($trustSha[0])" -notmatch '^[0-9a-f]{64}$') {
        throw "Test transcript-ready permit fixture generation failed."
    }
    $script:expectedPermitTrustRootSha256 = "$($trustSha[0])"
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
        -TranscriptServiceBaseUrl "https://transcript.internal.example" `
        -TranscriptServiceCapabilityPathTemplate "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}/analysis-capability" `
        -TranscriptServiceTokenUrl "https://auth.internal.example/oauth2/token" `
        -TranscriptServiceClientSecret $secureTranscriptCredential `
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
    Assert-True ($configText.Contains("MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI=")) `
        "Default-off durable delivery capability credential is missing."

    $loaded = Import-MeetingAiRuntimeEnvironment -Path $configPath
    Assert-True $loaded "Runtime config import must succeed."
    Assert-True ($env:MAI_MEETING_SERVICE_CLIENT_SECRET -eq $plainTestCredential) `
        "DPAPI client credential round trip failed."
    Assert-True ($env:MAI_MEETING_SERVICE_CLIENT_ID -eq "meeting-ai-ci") `
        "Non-secret runtime value import failed."
    Assert-True ($env:MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET -eq $plainTranscriptCredential) `
        "Default-off delivery capability credential round trip failed."
    Assert-PythonRuntimeConfigLoads

    $legacyUpgradeConfigPath = Join-Path $runtimeRoot "legacy-upgrade.env"
    $legacyUpgradeStorePath = Join-Path $runtimeRoot `
        "legacy-upgrade\analysis-delivery.sqlite3"
    $legacyUpgradeMaterialPath = Join-Path $runtimeRoot "legacy-upgrade-material.json"
    $legacyUpgradeProbe = Join-Path $PSScriptRoot `
        "exercise-meeting-ai-legacy-upgrade.py"
    $currentValues = Read-MeetingAiConfigFile -Path $configPath
    $legacyValues = Copy-ConfigValues -Values $currentValues
    $legacyActiveKeyId = $legacyValues["MAI_INGESTION_ACTIVE_KEY_ID"]
    $legacyClientSecretBlob = `
        $legacyValues["MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"]
    $currentKeyringJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $legacyValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    $legacyKeyring = $currentKeyringJson | ConvertFrom-Json -ErrorAction Stop
    $legacyKeyring.PSObject.Properties.Remove(
        $legacyValues["MAI_INGESTION_LOOKUP_KEY_ID"]
    )
    $legacyKeyringJson = $legacyKeyring | ConvertTo-Json -Compress
    $legacyValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] = `
        Protect-MeetingAiSecret -PlainText $legacyKeyringJson
    $legacyValues["MAI_INGESTION_STORE_PATH"] = $legacyUpgradeStorePath
    foreach ($name in @($legacyValues.Keys)) {
        if ($name -eq "MAI_APP_ENV" -or
            $name -eq "MAI_INGESTION_LOOKUP_KEY_ID" -or
            $name -eq "MAI_ANALYSIS_SPEC_VERSION" -or
            $name.StartsWith("MAI_READY_", [StringComparison]::OrdinalIgnoreCase) -or
            $name.StartsWith("MAI_TRANSCRIPT_", [StringComparison]::OrdinalIgnoreCase)) {
            $legacyValues.Remove($name)
        }
    }
    Write-MeetingAiSecretFileAtomic `
        -Path $legacyUpgradeConfigPath `
        -Content (ConvertTo-TestMeetingAiConfigContent -Values $legacyValues)
    $legacyMaterial = [ordered]@{
        activeKeyId = $legacyActiveKeyId
        keys = $legacyKeyring
    }
    Write-MeetingAiSecretFileAtomic `
        -Path $legacyUpgradeMaterialPath `
        -Content (($legacyMaterial | ConvertTo-Json -Depth 4 -Compress) + "`n")
    & $pythonExe $legacyUpgradeProbe seed `
        --store $legacyUpgradeStorePath --material $legacyUpgradeMaterialPath
    if ($LASTEXITCODE -ne 0) { throw "Legacy v1 outbox fixture seed failed." }

    & $configureScript `
        -ClientId "meeting-ai-ci" `
        -TranscriptServiceBaseUrl "https://transcript.internal.example" `
        -TranscriptServiceCapabilityPathTemplate "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}/analysis-capability" `
        -TranscriptServiceTokenUrl "https://auth.internal.example/oauth2/token" `
        -TranscriptServiceClientSecret $secureTranscriptCredential `
        -StorePath $legacyUpgradeStorePath `
        -ConfigPath $legacyUpgradeConfigPath `
        -Confirm:$false
    $upgradedValues = Read-MeetingAiConfigFile -Path $legacyUpgradeConfigPath
    Assert-MeetingAiConfigValues -Values $upgradedValues
    Assert-True ($upgradedValues["MAI_INGESTION_ACTIVE_KEY_ID"] -eq
        $legacyActiveKeyId) "Legacy upgrade must preserve the active payload key id."
    Assert-True ($upgradedValues["MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI"] -eq
        $legacyClientSecretBlob) "Legacy upgrade must preserve the meeting-service credential."
    Assert-True ($upgradedValues.ContainsKey("MAI_INGESTION_LOOKUP_KEY_ID")) `
        "Legacy upgrade must add a dedicated lookup key."
    $upgradedKeyringJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $upgradedValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    $upgradedKeyring = $upgradedKeyringJson | ConvertFrom-Json -ErrorAction Stop
    Assert-True (
        [string]$upgradedKeyring.PSObject.Properties[$legacyActiveKeyId].Value -eq
        [string]$legacyKeyring.PSObject.Properties[$legacyActiveKeyId].Value
    ) "Legacy upgrade must preserve retained-row decryption material."
    $upgradedMaterial = [ordered]@{
        activeKeyId = $upgradedValues["MAI_INGESTION_ACTIVE_KEY_ID"]
        lookupKeyId = $upgradedValues["MAI_INGESTION_LOOKUP_KEY_ID"]
        keys = $upgradedKeyring
    }
    Write-MeetingAiSecretFileAtomic `
        -Path $legacyUpgradeMaterialPath `
        -Content (($upgradedMaterial | ConvertTo-Json -Depth 4 -Compress) + "`n")
    & $pythonExe $legacyUpgradeProbe verify `
        --store $legacyUpgradeStorePath --material $legacyUpgradeMaterialPath
    if ($LASTEXITCODE -ne 0) { throw "Legacy v1 outbox upgrade verification failed." }
    Assert-True (Import-MeetingAiRuntimeEnvironment -Path $legacyUpgradeConfigPath) `
        "Upgraded legacy runtime config import must succeed."
    Assert-PythonRuntimeConfigLoads
    foreach ($path in @(
            $legacyUpgradeConfigPath,
            "$legacyUpgradeConfigPath.bak",
            $legacyUpgradeMaterialPath,
            $legacyUpgradeStorePath,
            "$legacyUpgradeStorePath-wal",
            "$legacyUpgradeStorePath-shm"
        )) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
    $currentKeyringJson = $null
    $legacyKeyringJson = $null
    $upgradedKeyringJson = $null
    Assert-True (Import-MeetingAiRuntimeEnvironment -Path $configPath) `
        "Primary runtime config must be restored after legacy upgrade proof."
    Assert-PythonRuntimeConfigLoads
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_MEETING_SERVICE_CLIENT_SECRET_DPAPI
    )) "DPAPI blob must not be exported to the child environment."

    $env:MAI_INGESTION_ENABLED = "true"
    $env:MAI_READY_CONSUMER_ENABLED = "true"
    & $startScript `
        -RepoRoot $repoRoot `
        -Backend "mock" `
        -AppEnv "test" `
        -RuntimeConfigPath (Join-Path $runtimeRoot "missing-startup.env") `
        -PythonExe "not-invoked.exe" `
        -ValidateConfigurationOnly
    Assert-True ($env:MAI_INGESTION_ENABLED -eq "false") `
        "Missing test config must clear inherited ingestion enablement."
    Assert-True ($env:MAI_READY_CONSUMER_ENABLED -eq "false") `
        "Missing test config must clear inherited ready-consumer enablement."

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
    Write-FreshPermit -Path $permitSource

    & $configureScript `
        -ReadyConsumerEnabled "true" `
        -RuntimeAppEnv "test" `
        -ReadyRedisUrl $secureReadyRedisUrl `
        -ReadyRedisStream $customReadyStream `
        -ReadyRedisGroup $customReadyGroup `
        -ReadyProducerReplayHorizonSec 2592000 `
        -AnalysisSpecVersion $customAnalysisSpec `
        -TranscriptServiceBaseUrl "https://transcript.internal.example" `
        -TranscriptServiceSnapshotPathTemplate "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}" `
        -TranscriptServiceCapabilityPathTemplate "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}/sessions/{session_id}/finalizations/{finalization_version}/analysis-capability" `
        -TranscriptServiceTokenUrl "https://auth.internal.example/oauth2/token" `
        -TranscriptServiceClientId $customTranscriptClientId `
        -TranscriptServiceClientSecret $secureTranscriptCredential `
        -ReadyPermitSourcePath $permitSource `
        -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
        -ExpectedGitopsCommit $expectedGitopsCommit `
        -ExpectedPolicySha256 $expectedPolicySha256 `
        -ExpectedProducerImageDigest $expectedProducerDigest `
        -ExpectedPermitTrustRootSha256 $expectedPermitTrustRootSha256 `
        -PythonExe $pythonExe `
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
        -StartupScriptPath $startScript `
        -PythonExe $pythonExe

    $installedPermit = $env:MAI_READY_PRE_ENABLE_PERMIT_PATH
    $installedPermitTrustRoot = $env:MAI_READY_PERMIT_TRUST_ROOT_PATH
    $installedReceipt = $env:MAI_READY_ACTIVATION_RECEIPT_PATH
    Assert-True (-not (Test-Path -LiteralPath $permitSource)) `
        "A successful activation must consume the permit source."
    Assert-True (Test-Path -LiteralPath $permitTrustRootSource -PathType Leaf) `
        "Activation must preserve the out-of-band public trust-root source."
    Assert-True (Test-Path -LiteralPath $installedPermitTrustRoot -PathType Leaf) `
        "A successful activation must install the pinned permit trust root."
    Assert-True (Test-Path -LiteralPath $installedReceipt -PathType Leaf) `
        "A successful activation must write a durable activation receipt."
    Assert-TranscriptReadyActivationReceiptFile `
        -ReceiptPath $installedReceipt `
        -PermitPath $installedPermit `
        -TrustRootPath $installedPermitTrustRoot `
        -ExpectedTrustRootSha256 $expectedPermitTrustRootSha256 `
        -ExpectedGitopsCommit $expectedGitopsCommit `
        -ExpectedPolicySha256 $expectedPolicySha256 `
        -ExpectedProducerImageDigest $expectedProducerDigest `
        -RepoRoot $repoRoot `
        -StartupScriptPath $startScript `
        -PythonExe $pythonExe `
        -AppEnv "test"

    $trustedPermitContent = [IO.File]::ReadAllText($installedPermit)
    $tamperedEnvelope = $trustedPermitContent | ConvertFrom-Json
    $trustedSignature = [string]$tamperedEnvelope.signatures[0].sig
    $tamperedEnvelope.signatures[0].sig = if ($trustedSignature[0] -eq "A") {
        "B" + $trustedSignature.Substring(1)
    } else {
        "A" + $trustedSignature.Substring(1)
    }
    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit `
        -Content (($tamperedEnvelope | ConvertTo-Json -Depth 8 -Compress) + "`n")
    Assert-ThrowsLike {
        Assert-TranscriptReadyPreEnablePermit `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startScript `
            -PythonExe $pythonExe
    } "signed permit verification failed"
    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit -Content $trustedPermitContent

    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit `
        -Content ($trustedPermitContent + " ")
    Assert-ThrowsLike {
        Assert-TranscriptReadyPreEnablePermit `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startScript `
            -PythonExe $pythonExe
    } "activation receipt binding does not match"
    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit `
        -Content $trustedPermitContent

    Assert-ThrowsLike {
        Assert-TranscriptReadyPermitFile `
            -PermitPath $installedPermit `
            -TrustRootPath $installedPermitTrustRoot `
            -ExpectedTrustRootSha256 (("e" * 64) -join "") `
            -ExpectedGitopsCommit $expectedGitopsCommit `
            -ExpectedPolicySha256 $expectedPolicySha256 `
            -ExpectedProducerImageDigest $expectedProducerDigest `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startScript `
            -PythonExe $pythonExe `
            -AppEnv "test"
    } "signed permit verification failed"

    Assert-ThrowsLike {
        Assert-TranscriptReadyPermitFile `
            -PermitPath $installedPermit `
            -TrustRootPath $installedPermitTrustRoot `
            -ExpectedTrustRootSha256 $expectedPermitTrustRootSha256 `
            -ExpectedGitopsCommit $expectedGitopsCommit `
            -ExpectedPolicySha256 $expectedPolicySha256 `
            -ExpectedProducerImageDigest $expectedProducerDigest `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startScript `
            -PythonExe $pythonExe `
            -AppEnv "stage"
    } "signed permit verification failed"

    $configBeforeReplay = [IO.File]::ReadAllBytes($configPath)
    [IO.File]::WriteAllBytes($permitSource, [IO.File]::ReadAllBytes($installedPermit))
    Assert-ThrowsLike {
        & $configureScript `
            -RotateEncryptionKey `
            -ReadyPermitSourcePath $permitSource `
            -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
            -ExpectedPermitTrustRootSha256 $expectedPermitTrustRootSha256 `
            -PythonExe $pythonExe `
            -StorePath $storePath `
            -ConfigPath $configPath `
            -Confirm:$false
    } "already consumed"
    Assert-True (-not (Test-Path -LiteralPath $permitSource)) `
        "A replayed permit source must be consumed while being rejected."
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -eq
        [Convert]::ToBase64String($configBeforeReplay)) `
        "A replayed permit must not mutate the active config."
    Assert-ThrowsLike {
        & $startScript `
            -RepoRoot $repoRoot `
            -Backend "mock" `
            -AppEnv "stage" `
            -RuntimeConfigPath $configPath `
            -PythonExe "not-invoked.exe" `
            -ValidateConfigurationOnly
    } "environment does not match the launcher"
    & $startScript `
        -RepoRoot $repoRoot `
        -Backend "mock" `
        -AppEnv "test" `
        -RuntimeConfigPath $configPath `
        -PythonExe $pythonExe `
        -ValidateConfigurationOnly

    $configPyPath = Join-Path $repoRoot `
        "services\meeting-ai-service\app\core\config.py"
    $configPyBytes = [IO.File]::ReadAllBytes($configPyPath)
    try {
        [IO.File]::AppendAllText(
            $configPyPath,
            "`n# ci tracked dirty probe`n",
            (New-Object Text.UTF8Encoding($false))
        )
        Assert-ThrowsLike {
            Assert-TranscriptReadyPreEnablePermit `
                -RepoRoot $repoRoot `
                -StartupScriptPath $startScript `
                -PythonExe $pythonExe
        } "worktree is not clean"
    } finally {
        [IO.File]::WriteAllBytes($configPyPath, $configPyBytes)
    }

    $untrackedProbe = Join-Path $repoRoot `
        "services\meeting-ai-service\app\ci-untracked-probe.py"
    try {
        [IO.File]::WriteAllText(
            $untrackedProbe,
            "raise RuntimeError('must never execute')`n",
            (New-Object Text.UTF8Encoding($false))
        )
        Assert-ThrowsLike {
            Assert-TranscriptReadyPreEnablePermit `
                -RepoRoot $repoRoot `
                -StartupScriptPath $startScript `
                -PythonExe $pythonExe
        } "untracked deployed content"
    } finally {
        if (Test-Path -LiteralPath $untrackedProbe) {
            Remove-Item -LiteralPath $untrackedProbe -Force
        }
    }

    $dotenvProbe = Join-Path $repoRoot "services\meeting-ai-service\.env"
    $permitBeforeDotenvProbe = [IO.File]::ReadAllText($installedPermit)
    Write-FreshPermit -Path $installedPermit -AppEnv "stage"
    try {
        [IO.File]::WriteAllText(
            $dotenvProbe,
            "MAI_READY_CONSUMER_ENABLED=true`n",
            (New-Object Text.UTF8Encoding($false))
        )
        Assert-ThrowsLike {
            Assert-TranscriptReadyPermitFile `
                -PermitPath $installedPermit `
                -TrustRootPath $installedPermitTrustRoot `
                -ExpectedTrustRootSha256 $expectedPermitTrustRootSha256 `
                -ExpectedGitopsCommit $expectedGitopsCommit `
                -ExpectedPolicySha256 $expectedPolicySha256 `
                -ExpectedProducerImageDigest $expectedProducerDigest `
                -RepoRoot $repoRoot `
                -StartupScriptPath $startScript `
                -PythonExe $pythonExe `
                -AppEnv "stage"
        } "forbidden dotenv source"
    } finally {
        if (Test-Path -LiteralPath $dotenvProbe) {
            Remove-Item -LiteralPath $dotenvProbe -Force
        }
        Write-MeetingAiSecretFileAtomic `
            -Path $installedPermit `
            -Content $permitBeforeDotenvProbe
    }

    $freshInstalledPermitContent = [IO.File]::ReadAllText($installedPermit)
    $freshInstalledReceiptContent = [IO.File]::ReadAllText($installedReceipt)
    Write-FreshPermit -Path $installedPermit -GeneratedAtOffsetSeconds -1200
    $staleEnvelope = [IO.File]::ReadAllText($installedPermit) | ConvertFrom-Json
    $stalePayloadJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([string]$staleEnvelope.payload)
    )
    $stalePayload = $stalePayloadJson | ConvertFrom-Json
    $staleReceipt = ($freshInstalledReceiptContent | ConvertFrom-Json)
    $staleReceipt.permitEnvelopeSha256 = Get-MeetingAiFileSha256 `
        -Path $installedPermit `
        -Purpose "Stale activated permit test fixture"
    $staleReceipt.liveTranscriptObservedAt = `
        $stalePayload.binding.liveTranscriptPod.observedAt
    Write-MeetingAiSecretFileAtomic `
        -Path $installedReceipt `
        -Content (($staleReceipt | ConvertTo-Json -Depth 8) + "`n")
    Assert-ThrowsLike {
        Assert-TranscriptReadyPermitFile `
            -PermitPath $installedPermit `
            -TrustRootPath $installedPermitTrustRoot `
            -ExpectedTrustRootSha256 $expectedPermitTrustRootSha256 `
            -ExpectedGitopsCommit $expectedGitopsCommit `
            -ExpectedPolicySha256 $expectedPolicySha256 `
            -ExpectedProducerImageDigest $expectedProducerDigest `
            -RepoRoot $repoRoot `
            -StartupScriptPath $startScript `
            -PythonExe $pythonExe `
            -AppEnv "test"
    } "signed permit verification failed"
    Assert-TranscriptReadyPreEnablePermit `
        -RepoRoot $repoRoot `
        -StartupScriptPath $startScript `
        -PythonExe $pythonExe
    $configBeforeRejectedMaintenance = [IO.File]::ReadAllBytes($configPath)
    Assert-ThrowsLike {
        & $configureScript `
            -RotateEncryptionKey `
            -StorePath $storePath `
            -ConfigPath $configPath `
            -Confirm:$false
    } "requires a fresh signed permit and trust root"
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -eq
        [Convert]::ToBase64String($configBeforeRejectedMaintenance)) `
        "Rejected enabled maintenance must not mutate the config."
    Write-MeetingAiSecretFileAtomic `
        -Path $installedPermit `
        -Content $freshInstalledPermitContent
    Write-MeetingAiSecretFileAtomic `
        -Path $installedReceipt `
        -Content $freshInstalledReceiptContent

    Write-FreshPermit -Path $permitSource
    $activePermitRoot = Split-Path -Parent $installedPermit
    $activationReceiptRoot = Split-Path -Parent $installedReceipt
    $consumptionRoot = Join-Path $runtimeRoot "permits\consumed"
    $permitFilesBeforeFailure = @(Get-ChildItem -LiteralPath $activePermitRoot -File)
    $receiptFilesBeforeFailure = @(Get-ChildItem -LiteralPath $activationReceiptRoot -File)
    $consumptionFilesBeforeFailure = @(Get-ChildItem -LiteralPath $consumptionRoot -File)
    $configBeforeInjectedFailure = [IO.File]::ReadAllBytes($configPath)
    $env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE = "1"
    try {
        Assert-ThrowsLike {
            & $configureScript `
                -RotateEncryptionKey `
                -ReadyPermitSourcePath $permitSource `
                -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
                -ExpectedPermitTrustRootSha256 $expectedPermitTrustRootSha256 `
                -PythonExe $pythonExe `
                -StorePath $storePath `
                -ConfigPath $configPath `
                -Confirm:$false
        } "TEST_INJECTED_MEETING_AI_CONFIG_WRITE_FAILURE"
    } finally {
        Remove-Item Env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE `
            -ErrorAction SilentlyContinue
    }
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -eq
        [Convert]::ToBase64String($configBeforeInjectedFailure)) `
        "Injected write failure must preserve the previous config."
    Assert-True (Test-Path -LiteralPath $installedPermit -PathType Leaf) `
        "Injected write failure must preserve the previous permit."
    Assert-True (Test-Path -LiteralPath $installedReceipt -PathType Leaf) `
        "Injected write failure must preserve the previous activation receipt."
    Assert-True (-not (Test-Path -LiteralPath $permitSource)) `
        "A failed activation must still consume its one-use source."
    $permitFilesAfterFailure = @(Get-ChildItem -LiteralPath $activePermitRoot -File)
    $receiptFilesAfterFailure = @(Get-ChildItem -LiteralPath $activationReceiptRoot -File)
    $consumptionFilesAfterFailure = @(Get-ChildItem -LiteralPath $consumptionRoot -File)
    Assert-True ($permitFilesAfterFailure.Count -eq $permitFilesBeforeFailure.Count) `
        "Injected write failure must clean the staged permit."
    Assert-True ($receiptFilesAfterFailure.Count -eq $receiptFilesBeforeFailure.Count) `
        "Injected write failure must clean the staged activation receipt."
    Assert-True ($consumptionFilesAfterFailure.Count -eq
        ($consumptionFilesBeforeFailure.Count + 1)) `
        "Injected write failure must retain the one-use consumption record."

    Start-Sleep -Milliseconds 20
    Write-FreshPermit -Path $permitSource
    $oldPermit = $installedPermit
    $oldPermitTrustRoot = $installedPermitTrustRoot
    $oldReceipt = $installedReceipt
    & $configureScript `
        -RotateEncryptionKey `
        -ReadyPermitSourcePath $permitSource `
        -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
        -ExpectedPermitTrustRootSha256 $expectedPermitTrustRootSha256 `
        -PythonExe $pythonExe `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $rotatedReadyValues = Read-MeetingAiConfigFile -Path $configPath
    Assert-True ($rotatedReadyValues["MAI_READY_REDIS_STREAM"] -eq $customReadyStream) `
        "Ready Redis stream must survive enabled maintenance."
    Assert-True ($rotatedReadyValues["MAI_READY_REDIS_GROUP"] -eq $customReadyGroup) `
        "Ready Redis group must survive enabled maintenance."
    Assert-True ($rotatedReadyValues["MAI_ANALYSIS_SPEC_VERSION"] -eq `
        $customAnalysisSpec) "Analysis spec must survive enabled maintenance."
    Assert-True ($rotatedReadyValues["MAI_TRANSCRIPT_SERVICE_CLIENT_ID"] -eq `
        $customTranscriptClientId) `
        "Transcript client identity must survive enabled maintenance."
    $installedPermit = $rotatedReadyValues["MAI_READY_PRE_ENABLE_PERMIT_PATH"]
    $installedPermitTrustRoot = `
        $rotatedReadyValues["MAI_READY_PERMIT_TRUST_ROOT_PATH"]
    $installedReceipt = $rotatedReadyValues["MAI_READY_ACTIVATION_RECEIPT_PATH"]
    Assert-True (Test-Path -LiteralPath $installedPermit -PathType Leaf) `
        "Successful enabled maintenance must activate the new permit."
    Assert-True (Test-Path -LiteralPath $installedReceipt -PathType Leaf) `
        "Successful enabled maintenance must activate a new receipt."
    Assert-True (Test-Path -LiteralPath $installedPermitTrustRoot -PathType Leaf) `
        "Successful enabled maintenance must retain the pinned trust root."
    if ($oldPermit -ne $installedPermit) {
        Assert-True (-not (Test-Path -LiteralPath $oldPermit -PathType Leaf)) `
            "Successful enabled maintenance must revoke the previous permit."
    }
    if ($oldReceipt -ne $installedReceipt) {
        Assert-True (-not (Test-Path -LiteralPath $oldReceipt -PathType Leaf)) `
            "Successful enabled maintenance must revoke the previous receipt."
    }
    Assert-True ($oldPermitTrustRoot -eq $installedPermitTrustRoot) `
        "Permit rotation must keep the content-addressed trust root stable."

    Start-Sleep -Milliseconds 20
    Write-FreshPermit -Path $permitSource
    $preRestorePermit = $installedPermit
    $preRestorePermitTrustRoot = $installedPermitTrustRoot
    $preRestoreReceipt = $installedReceipt
    & $configureScript `
        -RestoreBackup `
        -ReadyPermitSourcePath $permitSource `
        -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
        -PythonExe $pythonExe `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $enabledRestoreValues = Read-MeetingAiConfigFile -Path $configPath
    Assert-True ($enabledRestoreValues["MAI_READY_CONSUMER_ENABLED"] -eq "true") `
        "Enabled-to-enabled restore must keep the ready consumer enabled."
    $installedPermit = $enabledRestoreValues["MAI_READY_PRE_ENABLE_PERMIT_PATH"]
    $installedPermitTrustRoot = `
        $enabledRestoreValues["MAI_READY_PERMIT_TRUST_ROOT_PATH"]
    $installedReceipt = $enabledRestoreValues["MAI_READY_ACTIVATION_RECEIPT_PATH"]
    Assert-True (Test-Path -LiteralPath $installedPermit -PathType Leaf) `
        "Enabled restore must activate a fresh permit."
    Assert-True (Test-Path -LiteralPath $installedReceipt -PathType Leaf) `
        "Enabled restore must activate a fresh receipt."
    Assert-True (Test-Path -LiteralPath $installedPermitTrustRoot -PathType Leaf) `
        "Enabled restore must retain the pinned trust root."
    Assert-True (-not (Test-Path -LiteralPath $preRestorePermit -PathType Leaf)) `
        "Enabled restore must revoke the superseded permit."
    Assert-True (-not (Test-Path -LiteralPath $preRestoreReceipt -PathType Leaf)) `
        "Enabled restore must revoke the superseded receipt."
    Assert-True ($preRestorePermitTrustRoot -eq $installedPermitTrustRoot) `
        "Enabled restore must preserve the content-addressed trust root."

    & $configureScript `
        -ReadyConsumerEnabled "false" `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $disabledConfigText = [IO.File]::ReadAllText($configPath)
    Assert-True ($disabledConfigText.Contains("MAI_READY_CONSUMER_ENABLED=false")) `
        "Ready consumer rollback must be explicitly disabled."
    Assert-True (-not $disabledConfigText.Contains("MAI_READY_REDIS_URL_DPAPI=")) `
        "Ready Redis DPAPI blob must be removed by rollback."
    Assert-True ($disabledConfigText.Contains(
        "MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI="
    )) "Rollback must retain the delivery-only transcript capability credential."
    Assert-True (-not $disabledConfigText.Contains(
        "MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE="
    )) "Rollback must remove the canonical transcript read path."
    Assert-True (-not $disabledConfigText.Contains(
        "MAI_TRANSCRIPT_SERVICE_SCOPE="
    )) "Rollback must remove the canonical transcript read scope."
    Assert-True (-not (Test-Path -LiteralPath $installedPermit -PathType Leaf)) `
        "Ready consumer rollback must revoke the installed permit."
    Assert-True (-not (Test-Path -LiteralPath $installedReceipt -PathType Leaf)) `
        "Ready consumer rollback must revoke the activation receipt."
    Assert-True (Test-Path -LiteralPath $installedPermitTrustRoot -PathType Leaf) `
        "Ready consumer rollback may retain the pinned public trust root."

    $disabledBeforeFailedRestore = [IO.File]::ReadAllBytes($configPath)
    Start-Sleep -Milliseconds 20
    Write-FreshPermit -Path $permitSource
    $env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE = "1"
    try {
        Assert-ThrowsLike {
            & $configureScript `
                -RestoreBackup `
                -ReadyPermitSourcePath $permitSource `
                -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
                -PythonExe $pythonExe `
                -StorePath $storePath `
                -ConfigPath $configPath `
                -Confirm:$false
        } "TEST_INJECTED_MEETING_AI_CONFIG_WRITE_FAILURE"
    } finally {
        Remove-Item Env:PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE `
            -ErrorAction SilentlyContinue
    }
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -eq
        [Convert]::ToBase64String($disabledBeforeFailedRestore)) `
        "Failed disabled-to-enabled restore must preserve the disabled config."

    Start-Sleep -Milliseconds 20
    Write-FreshPermit -Path $permitSource
    & $configureScript `
        -RestoreBackup `
        -ReadyPermitSourcePath $permitSource `
        -ReadyPermitTrustRootSourcePath $permitTrustRootSource `
        -PythonExe $pythonExe `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $restoredEnabledValues = Read-MeetingAiConfigFile -Path $configPath
    Assert-True ($restoredEnabledValues["MAI_READY_CONSUMER_ENABLED"] -eq "true") `
        "Disabled-to-enabled restore must activate the ready consumer."
    $installedPermit = $restoredEnabledValues["MAI_READY_PRE_ENABLE_PERMIT_PATH"]
    $installedPermitTrustRoot = `
        $restoredEnabledValues["MAI_READY_PERMIT_TRUST_ROOT_PATH"]
    $installedReceipt = $restoredEnabledValues["MAI_READY_ACTIVATION_RECEIPT_PATH"]
    Assert-True (Test-Path -LiteralPath $installedPermit -PathType Leaf) `
        "Disabled-to-enabled restore must install a fresh permit."
    Assert-True (Test-Path -LiteralPath $installedReceipt -PathType Leaf) `
        "Disabled-to-enabled restore must install a fresh receipt."
    Assert-True (Test-Path -LiteralPath $installedPermitTrustRoot -PathType Leaf) `
        "Disabled-to-enabled restore must install the pinned trust root."

    & $configureScript `
        -RestoreBackup `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $restoredDisabledValues = Read-MeetingAiConfigFile -Path $configPath
    Assert-True ($restoredDisabledValues["MAI_READY_CONSUMER_ENABLED"] -eq "false") `
        "Enabled-to-disabled restore must restore the disabled config."
    Assert-True (-not (Test-Path -LiteralPath $installedPermit -PathType Leaf)) `
        "Enabled-to-disabled restore must revoke the permit."
    Assert-True (-not (Test-Path -LiteralPath $installedReceipt -PathType Leaf)) `
        "Enabled-to-disabled restore must revoke the receipt."
    Assert-True (Test-Path -LiteralPath $installedPermitTrustRoot -PathType Leaf) `
        "Enabled-to-disabled restore may retain the pinned public trust root."
    Assert-True (Import-MeetingAiRuntimeEnvironment -Path $configPath) `
        "Disabled ready consumer runtime config import must succeed."
    Assert-True ([string]::IsNullOrWhiteSpace($env:MAI_READY_REDIS_URL)) `
        "Ready Redis credential must be cleared from process memory on rollback."
    Assert-True ($env:MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET -eq $plainTranscriptCredential) `
        "Rollback must retain the delivery-only transcript capability credential."
    Assert-PythonRuntimeConfigLoads
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_TRANSCRIPT_SERVICE_SNAPSHOT_PATH_TEMPLATE
    )) "Canonical transcript read path must be cleared on rollback."
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_TRANSCRIPT_SERVICE_SCOPE
    )) "Canonical transcript read scope must be cleared on rollback."
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_READY_PRE_ENABLE_PERMIT_PATH
    )) "Ready permit binding must be cleared from process memory on rollback."
    Assert-True ([string]::IsNullOrWhiteSpace(
        $env:MAI_READY_PERMIT_TRUST_ROOT_PATH
    )) "Ready permit trust-root binding must be cleared from process memory on rollback."

    & $configureScript `
        -RuntimeAppEnv "stage" `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
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
    $beforeLookup = $beforeValues["MAI_INGESTION_LOOKUP_KEY_ID"]
    $beforeJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $beforeValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    $beforeKeyring = $beforeJson | ConvertFrom-Json
    $beforeCount = @($beforeKeyring.PSObject.Properties).Count
    $beforeLookupValue = [string]$beforeKeyring.PSObject.Properties[$beforeLookup].Value

    $keyringProbe = Join-Path $PSScriptRoot "exercise-meeting-ai-keyring.py"
    $keyringMaterialPath = Join-Path $runtimeRoot "meeting-ai-keyring-probe.json"
    function Write-KeyringProbeMaterial {
        param($Values)

        $json = Unprotect-MeetingAiSecret `
            -ProtectedBase64 $Values["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
            -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
        $material = [ordered]@{
            activeKeyId = $Values["MAI_INGESTION_ACTIVE_KEY_ID"]
            lookupKeyId = $Values["MAI_INGESTION_LOOKUP_KEY_ID"]
            keys = ($json | ConvertFrom-Json)
        }
        Write-MeetingAiSecretFileAtomic `
            -Path $keyringMaterialPath `
            -Content (($material | ConvertTo-Json -Depth 4 -Compress) + "`n")
        $json = $null
        $material = $null
    }

    Write-KeyringProbeMaterial -Values $beforeValues
    & $pythonExe $keyringProbe seed `
        --store $storePath --keyring $keyringMaterialPath --expected-active $beforeActive
    if ($LASTEXITCODE -ne 0) { throw "Initial retained-row keyring probe failed." }

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
    Assert-True ($rotatedValues["MAI_INGESTION_LOOKUP_KEY_ID"] -eq $beforeLookup) `
        "Payload DEK rotation must preserve the blind-index key id."
    $rotatedKeyring = $rotatedJson | ConvertFrom-Json
    Assert-True (
        [string]$rotatedKeyring.PSObject.Properties[$beforeLookup].Value -eq $beforeLookupValue
    ) "Payload DEK rotation must preserve the blind-index key material."
    Assert-True (Test-Path -LiteralPath "$configPath.bak") `
        "Atomic rotation backup is missing."
    Assert-MeetingAiAcl -Path "$configPath.bak"

    Write-KeyringProbeMaterial -Values $rotatedValues
    & $pythonExe $keyringProbe verify `
        --store $storePath --keyring $keyringMaterialPath `
        --expected-active $rotatedValues["MAI_INGESTION_ACTIVE_KEY_ID"]
    if ($LASTEXITCODE -ne 0) { throw "Rotated retained-row keyring probe failed." }

    & $configureScript `
        -RestoreBackup `
        -StorePath $storePath `
        -ConfigPath $configPath `
        -Confirm:$false
    $restoredValues = Read-MeetingAiConfigFile -Path $configPath
    Assert-True ($restoredValues["MAI_INGESTION_ACTIVE_KEY_ID"] -eq $beforeActive) `
        "Backup restore did not restore the previous active key."
    Assert-True ($restoredValues["MAI_INGESTION_LOOKUP_KEY_ID"] -eq $beforeLookup) `
        "Backup restore did not preserve the blind-index key id."
    $restoredJson = Unprotect-MeetingAiSecret `
        -ProtectedBase64 $restoredValues["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] `
        -KeyName "MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"
    $restoredKeyring = $restoredJson | ConvertFrom-Json
    $rotatedActive = $rotatedValues["MAI_INGESTION_ACTIVE_KEY_ID"]
    Assert-True ($null -ne $restoredKeyring.PSObject.Properties[$rotatedActive]) `
        "Backup restore must retain the post-rotation DEK for database rollback."
    Write-KeyringProbeMaterial -Values $restoredValues
    & $pythonExe $keyringProbe verify `
        --store $storePath --keyring $keyringMaterialPath --expected-active $beforeActive
    if ($LASTEXITCODE -ne 0) { throw "Restored retained-row keyring probe failed." }
    Remove-Item -LiteralPath $keyringMaterialPath -Force

    function Write-RestoreConflictFixture {
        param(
            [Parameter(Mandatory = $true)][string]$Path,
            [Parameter(Mandatory = $true)]$CurrentValues,
            [Parameter(Mandatory = $true)]$BackupValues
        )

        Write-MeetingAiSecretFileAtomic `
            -Path $Path `
            -Content (ConvertTo-TestMeetingAiConfigContent -Values $CurrentValues)
        Write-MeetingAiSecretFileAtomic `
            -Path "$Path.bak" `
            -Content (ConvertTo-TestMeetingAiConfigContent -Values $BackupValues)
    }

    $lookupConflictPath = Join-Path $runtimeRoot "lookup-conflict.env"
    $lookupConflictCurrent = Copy-ConfigValues -Values $restoredValues
    $lookupConflictBackup = Copy-ConfigValues -Values $restoredValues
    $lookupConflictBackup["MAI_INGESTION_LOOKUP_KEY_ID"] = $rotatedActive
    Write-RestoreConflictFixture `
        -Path $lookupConflictPath `
        -CurrentValues $lookupConflictCurrent `
        -BackupValues $lookupConflictBackup
    Assert-ThrowsLike {
        & $configureScript `
            -RestoreBackup `
            -StorePath $storePath `
            -ConfigPath $lookupConflictPath `
            -Confirm:$false
    } "cannot cross an unversioned blind-index key change"

    $materialConflictPath = Join-Path $runtimeRoot "material-conflict.env"
    $materialConflictCurrent = Copy-ConfigValues -Values $restoredValues
    $materialConflictBackup = Copy-ConfigValues -Values $restoredValues
    $materialConflictKeyring = $restoredJson | ConvertFrom-Json
    $differentKeyBytes = New-Object byte[] 32
    for ($index = 0; $index -lt $differentKeyBytes.Length; $index++) {
        $differentKeyBytes[$index] = 165
    }
    try {
        $materialConflictKeyring.PSObject.Properties[$rotatedActive].Value = `
            [Convert]::ToBase64String($differentKeyBytes)
        $materialConflictBackup["MAI_INGESTION_ENCRYPTION_KEYS_JSON_DPAPI"] = `
            Protect-MeetingAiSecret -PlainText (
                $materialConflictKeyring | ConvertTo-Json -Compress
            )
    } finally {
        [Array]::Clear($differentKeyBytes, 0, $differentKeyBytes.Length)
    }
    Write-RestoreConflictFixture `
        -Path $materialConflictPath `
        -CurrentValues $materialConflictCurrent `
        -BackupValues $materialConflictBackup
    Assert-ThrowsLike {
        & $configureScript `
            -RestoreBackup `
            -StorePath $storePath `
            -ConfigPath $materialConflictPath `
            -Confirm:$false
    } "conflicting material for key id"
    foreach ($path in @(
            $lookupConflictPath,
            "$lookupConflictPath.bak",
            $materialConflictPath,
            "$materialConflictPath.bak"
        )) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }

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

    $previousCulture = [Threading.Thread]::CurrentThread.CurrentCulture
    $previousUiCulture = [Threading.Thread]::CurrentThread.CurrentUICulture
    try {
        $turkishCulture = [Globalization.CultureInfo]::GetCultureInfo("tr-TR")
        [Threading.Thread]::CurrentThread.CurrentCulture = $turkishCulture
        [Threading.Thread]::CurrentThread.CurrentUICulture = $turkishCulture
        $turkishValues = Read-MeetingAiConfigFile -Path $configPath
        Assert-True ($turkishValues.ContainsKey("MAI_INGESTION_ENABLED")) `
            "ASCII runtime keys must remain valid under the Turkish culture."

        $invalidTurkish = Join-Path $runtimeRoot "invalid-turkish-key.env"
        $dottedUpperI = [char]0x0130
        $invalidTurkishText = $configText.Replace(
            "MAI_INGESTION_ENABLED",
            ("MA{0}_INGESTION_ENABLED" -f $dottedUpperI)
        )
        [IO.File]::WriteAllText(
            $invalidTurkish,
            $invalidTurkishText,
            (New-Object Text.UTF8Encoding($false))
        )
        Set-Acl -LiteralPath $invalidTurkish -AclObject (New-MeetingAiAcl)
        Assert-ThrowsLike {
            Read-MeetingAiConfigFile -Path $invalidTurkish
        } "invalid key name"
    } finally {
        [Threading.Thread]::CurrentThread.CurrentCulture = $previousCulture
        [Threading.Thread]::CurrentThread.CurrentUICulture = $previousUiCulture
    }

    Write-Host "meeting-ai Windows runtime contract: PASS"
} finally {
    Clear-MeetingAiManagedProcessEnvironment
    $env:CI = $previousCi
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
    foreach ($artifact in @($permitTrustRootSource, $permitPrivateKeyPath)) {
        if (Test-Path -LiteralPath $artifact) {
            Remove-Item -LiteralPath $artifact -Force
        }
    }
}
