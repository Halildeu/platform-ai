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
            throw "Expected error containing '$Expected'."
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
    $secureCredential.Dispose()
    if (Test-Path -LiteralPath $runtimeRoot) {
        Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $tlsSourceRoot) {
        Remove-Item -LiteralPath $tlsSourceRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $startupProbeRoot) {
        Remove-Item -LiteralPath $startupProbeRoot -Recurse -Force
    }
}
