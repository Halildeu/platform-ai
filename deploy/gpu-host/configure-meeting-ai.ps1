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
    if (-not [string]::IsNullOrWhiteSpace($installedCaPath)) {
        $config["MAI_MEETING_SERVICE_TLS_CA_PATH"] = $installedCaPath
    }
    if ($effectiveTlsMode -eq "mutual") {
        $config["MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH"] = $installedCertPath
        $config["MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI"] = $clientKeyBlob
        $config["MAI_MEETING_SERVICE_TLS_RELOAD_INTERVAL_SEC"] = "60"
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
    Write-MeetingAiConfigAtomic -Path $ConfigPath -Content $content
    Write-Host "meeting-ai runtime config written: $ConfigPath"
    Write-Host "active encryption key id: $activeKeyId"
    Write-Host "Restart task with schtasks.exe /End and /Run for platform-ai-meeting-ai."
} finally {
    if ($lockTaken) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
