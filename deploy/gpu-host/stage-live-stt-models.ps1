# Stage both production live-STT model revisions before Scheduled Tasks exist.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [string]$RuntimeRoot = "",
    [string]$PolicyPath = "",
    [string]$TestSourceRoot = "",
    [ValidateRange(60, 14400)][int]$PerModelTimeoutSec = 3600
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$taskContract = Join-Path $PSScriptRoot "task-action-contract.ps1"
$processContract = Join-Path $PSScriptRoot "bootstrap-process.ps1"
$modelContract = Join-Path $PSScriptRoot "live-stt-model-runtime.ps1"
$helperPath = Join-Path $PSScriptRoot "stage-live-stt-model.py"
foreach ($required in @($taskContract, $processContract, $modelContract, $helperPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Missing live STT model staging dependency: $required"
    }
}
. $taskContract
. $processContract
. $modelContract

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run live STT model staging from an elevated Administrator PowerShell."
}

$PythonExe = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
$defaultPolicy = Join-Path $PSScriptRoot "live-stt-model-policy.json"
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Get-LiveSttDefaultModelRuntimeRoot
}
if ([string]::IsNullOrWhiteSpace($PolicyPath)) {
    $PolicyPath = $defaultPolicy
}
$allowTrustedCiPath = $false
if (-not (Test-GpuHostSameLocalPath -Left $PolicyPath -Right $defaultPolicy) -or
    -not (Test-GpuHostSameLocalPath -Left $RuntimeRoot `
        -Right (Get-LiveSttDefaultModelRuntimeRoot)) -or
    -not [string]::IsNullOrWhiteSpace($TestSourceRoot)) {
    $allowTrustedCiPath = (
        (Test-LiveSttTrustedCiPath -Path $RuntimeRoot) -and
        (Test-LiveSttTrustedCiPath -Path $PolicyPath) -and
        (-not [string]::IsNullOrWhiteSpace($TestSourceRoot)) -and
        (Test-LiveSttTrustedCiPath -Path $TestSourceRoot)
    )
    if (-not $allowTrustedCiPath) {
        throw "Custom model policy/source/runtime paths are restricted to the trusted hosted CI contract."
    }
}

$RuntimeRoot = Assert-LiveSttModelRootPath -RuntimeRoot $RuntimeRoot `
    -AllowTrustedCiPath:$allowTrustedCiPath
$PolicyPath = (Resolve-Path -LiteralPath $PolicyPath -ErrorAction Stop).Path
$policy = Read-LiveSttModelPolicy -PolicyPath $PolicyPath

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
foreach ($model in @($policy.models)) {
    $relative = ([string]$model.relativePath).Replace('/', '\')
    $parent = Split-Path -Parent (Join-Path $RuntimeRoot $relative)
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}
Set-LiveSttModelTreeAcl -Path $RuntimeRoot

foreach ($model in @($policy.models)) {
    $relative = ([string]$model.relativePath).Replace('/', '\')
    $destination = Join-Path $RuntimeRoot $relative
    $parent = Split-Path -Parent $destination
    $staging = Join-Path $parent (".{0}.{1}.staging" -f `
        $model.revision, [Guid]::NewGuid().ToString('N'))
    $download = Join-Path $parent (".{0}.download" -f (Split-Path -Leaf $staging))
    $backup = Join-Path $parent (".{0}.{1}.backup" -f `
        $model.revision, [Guid]::NewGuid().ToString('N'))
    $hasBackup = $false
    try {
        $alreadyValid = $false
        if (Test-Path -LiteralPath $destination -PathType Container) {
            try {
                Assert-LiveSttModelTreeAcl -Path $destination
                Invoke-LiveSttModelVerifier -PythonExe $PythonExe `
                    -HelperPath $helperPath -Model $model -Destination $destination
                $alreadyValid = $true
            } catch {
                Write-Host ("[models] replacing invalid {0} runtime revision" -f `
                    $model.role) -ForegroundColor Yellow
            }
        }
        if ($alreadyValid) {
            Write-Host ("[models] exact {0} revision already staged" -f $model.role)
            continue
        }

        $arguments = @(
            $helperPath,
            "stage",
            "--repository", [string]$model.repository,
            "--revision", [string]$model.revision,
            "--model-bin-sha256", [string]$model.modelBinSha256,
            "--destination", $staging
        )
        if (-not [string]::IsNullOrWhiteSpace($TestSourceRoot)) {
            $source = Join-Path $TestSourceRoot ([string]$model.role)
            $arguments += @("--source-directory", $source)
        }
        $stageResult = Invoke-GpuHostBoundedProcess -FileName $PythonExe `
            -ArgumentList $arguments -TimeoutSec $PerModelTimeoutSec `
            -Operation ("Stage {0} exact live-STT model" -f $model.role)
        if ($stageResult.ExitCode -ne 0) {
            throw "Live STT $($model.role) model staging failed."
        }
        Set-LiveSttModelTreeAcl -Path $staging
        Assert-LiveSttModelTreeAcl -Path $staging
        Invoke-LiveSttModelVerifier -PythonExe $PythonExe -HelperPath $helperPath `
            -Model $model -Destination $staging

        if (Test-Path -LiteralPath $destination) {
            Move-Item -LiteralPath $destination -Destination $backup -ErrorAction Stop
            $hasBackup = $true
        }
        Move-Item -LiteralPath $staging -Destination $destination -ErrorAction Stop
        Assert-LiveSttModelTreeAcl -Path $destination
        Invoke-LiveSttModelVerifier -PythonExe $PythonExe -HelperPath $helperPath `
            -Model $model -Destination $destination
        if ($hasBackup) {
            Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction Stop
            $hasBackup = $false
        }
        Write-Host ("[models] staged exact {0} revision {1}" -f `
            $model.role, $model.revision)
    } catch {
        $failure = $_.Exception.Message
        if ($hasBackup) {
            try {
                if (Test-Path -LiteralPath $destination) {
                    Remove-Item -LiteralPath $destination -Recurse -Force `
                        -ErrorAction Stop
                }
                Move-Item -LiteralPath $backup -Destination $destination `
                    -ErrorAction Stop
                $hasBackup = $false
            } catch {
                throw "$failure Model staging rollback failed: $($_.Exception.Message)"
            }
        }
        throw $failure
    } finally {
        foreach ($temporary in @($staging, $download, $backup)) {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Recurse -Force `
                    -ErrorAction SilentlyContinue
            }
        }
    }
}

$verified = Assert-LiveSttModelSet -RuntimeRoot $RuntimeRoot `
    -PythonExe $PythonExe -PolicyPath $PolicyPath -HelperPath $helperPath `
    -AllowTrustedCiPath:$allowTrustedCiPath
Write-Host ("[models] staged and verified {0} exact revisions under {1}" -f `
    @($verified.models).Count, $RuntimeRoot)
