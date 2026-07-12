# Windows PowerShell 5.1 behavior contract for the meeting-ai runtime config.
# Runs only on an ephemeral windows-latest GitHub runner. No value is printed.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$deployDir = Join-Path $repoRoot "deploy\gpu-host"
$runtimeScript = Join-Path $deployDir "meeting-ai-runtime-env.ps1"
$configureScript = Join-Path $deployDir "configure-meeting-ai.ps1"
. $runtimeScript

$runtimeRoot = Get-MeetingAiRuntimeRoot
$configPath = Join-Path $runtimeRoot "meeting-ai.env"
$storePath = Join-Path $runtimeRoot "meeting-ai\analysis-delivery.sqlite3"
$plainTestCredential = "ci-ephemeral-credential"
$secureCredential = ConvertTo-SecureString $plainTestCredential -AsPlainText -Force

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
    $env:MAI_MEETING_SERVICE_CLIENT_SECRET = $null
    $env:MAI_INGESTION_ENCRYPTION_KEYS_JSON = $null
    $secureCredential.Dispose()
    if (Test-Path -LiteralPath $runtimeRoot) {
        Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
    }
}
