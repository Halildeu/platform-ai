# Windows PowerShell 5.1 behavior contract for the test-only gateway hosts shim.
# It uses a custom fixture path and never mutates the runner's system hosts file.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$scriptPath = Join-Path $repoRoot "deploy\gpu-host\configure-private-gateway-host.ps1"
$fixtureRoot = Join-Path $env:RUNNER_TEMP "private-gateway-host-contract"
$hostsPath = Join-Path $fixtureRoot "hosts"
$targetHost = "meeting-ai-gateway.internal"
$targetIp = "10.99.0.1"
$flushCount = 0

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
                $Expected, $_.Exception.GetType().Name, $_.Exception.Message)
        }
        return
    }
    throw "Expected an exception containing '$Expected'."
}

function New-Fixture {
    param([string]$Content)
    [IO.File]::WriteAllText($hostsPath, $Content, (New-Object Text.UTF8Encoding($false)))
}

function Invoke-Shim {
    param(
        [scriptblock]$Resolver = { param($name) @("10.99.0.1") },
        [switch]$Remove,
        [switch]$RestoreBackup,
        [switch]$WhatIf,
        [int]$MutexTimeoutSeconds = 30
    )
    $arguments = @{
        TestHostShim = $true
        HostsPath = $hostsPath
        GatewayHostname = $targetHost
        GatewayIPv4 = $targetIp
        DnsFlushAction = { $script:flushCount += 1; return 0 }
        ResolverProbe = $Resolver
        Confirm = $false
        MutexTimeoutSeconds = $MutexTimeoutSeconds
    }
    if ($Remove) { $arguments["Remove"] = $true }
    if ($RestoreBackup) { $arguments["RestoreBackup"] = $true }
    if ($WhatIf) { $arguments["WhatIf"] = $true }
    & $scriptPath @arguments
}

try {
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null

    $original = "# baseline`r`n127.0.0.1 localhost`r`n10.20.30.40 other.internal # keep`r`n"
    New-Fixture -Content $original
    $aclBefore = (Get-Acl -LiteralPath $hostsPath).GetSecurityDescriptorSddlForm("All")
    Invoke-Shim

    $applied = [IO.File]::ReadAllText($hostsPath)
    Assert-True ($applied.Contains("# BEGIN platform-ai meeting-ai private gateway test shim")) `
        "Managed begin marker is missing."
    Assert-True ($applied.Contains("$targetIp $targetHost")) "Target mapping is missing."
    Assert-True ($applied.Contains("10.20.30.40 other.internal # keep")) `
        "Unrelated content was not preserved."
    Assert-True (([Regex]::Matches($applied, [Regex]::Escape($targetHost))).Count -eq 1) `
        "Target hostname must occur exactly once."
    $bytes = [IO.File]::ReadAllBytes($hostsPath)
    Assert-True (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and `
        $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) "Hosts file must not gain a BOM."
    $aclAfter = (Get-Acl -LiteralPath $hostsPath).GetSecurityDescriptorSddlForm("All")
    Assert-True ($aclAfter -eq $aclBefore) "Hosts file ACL changed after atomic replace."
    Assert-True ($flushCount -eq 1) "Apply must flush DNS exactly once."

    $firstBytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($hostsPath))
    Invoke-Shim
    $secondBytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($hostsPath))
    Assert-True ($firstBytes -eq $secondBytes) "Idempotent apply changed the hosts file."
    Assert-True ($flushCount -eq 2) "Idempotent apply must still verify live resolution."

    Invoke-Shim -RestoreBackup
    Assert-True ([IO.File]::ReadAllText($hostsPath) -eq $original) `
        "Backup restore did not recover the original fixture."

    New-Fixture -Content ("$original$targetIp $targetHost`r`n")
    Invoke-Shim
    $migrated = [IO.File]::ReadAllText($hostsPath)
    Assert-True (([Regex]::Matches($migrated, [Regex]::Escape($targetHost))).Count -eq 1) `
        "Legacy exact mapping was not adopted into one managed mapping."

    Invoke-Shim -Remove
    $removed = [IO.File]::ReadAllText($hostsPath)
    Assert-True (-not $removed.Contains($targetHost)) "Remove left the target hostname behind."
    Assert-True ($removed.Contains("127.0.0.1 localhost")) "Remove changed unrelated lines."

    New-Fixture -Content ("$original`r`n10.99.0.9 $targetHost`r`n")
    $beforeConflict = [IO.File]::ReadAllText($hostsPath)
    Assert-ThrowsLike { Invoke-Shim } "active mapping outside the managed block"
    Assert-True ([IO.File]::ReadAllText($hostsPath) -eq $beforeConflict) `
        "Conflict rejection mutated the fixture."

    New-Fixture -Content ("$original# 10.99.0.9 $targetHost`r`n")
    Invoke-Shim
    Assert-True ([IO.File]::ReadAllText($hostsPath).Contains("# 10.99.0.9 $targetHost")) `
        "Commented mapping must remain unrelated content."

    New-Fixture -Content $original
    $beforeMismatch = [IO.File]::ReadAllText($hostsPath)
    Assert-ThrowsLike { Invoke-Shim -Resolver { param($name) @("10.99.0.9") } } `
        "did not return exactly"
    Assert-True ([IO.File]::ReadAllText($hostsPath) -eq $beforeMismatch) `
        "Resolution failure did not roll back the hosts mutation."

    New-Fixture -Content $original
    $beforeWhatIf = [IO.File]::ReadAllText($hostsPath)
    Invoke-Shim -WhatIf
    Assert-True ([IO.File]::ReadAllText($hostsPath) -eq $beforeWhatIf) `
        "WhatIf mutated the fixture."

    foreach ($invalidIp in @("256.1.1.1", "10.099.0.1", "10.99.0.1/32", "::1")) {
        Assert-ThrowsLike {
            & $scriptPath -TestHostShim -HostsPath $hostsPath -GatewayIPv4 $invalidIp `
                -DnsFlushAction { 0 } -ResolverProbe { @($targetIp) } -Confirm:$false
        } "GatewayIPv4"
    }
    foreach ($invalidHost in @("meeting-ai", "meeting-ai.internal.", "bad#host.internal", `
        "bad host.internal", "bad`nhost.internal", "-bad.internal")) {
        Assert-ThrowsLike {
            & $scriptPath -TestHostShim -HostsPath $hostsPath `
                -GatewayHostname $invalidHost -DnsFlushAction { 0 } `
                -ResolverProbe { @($targetIp) } -Confirm:$false
        } "GatewayHostname"
    }

    New-Fixture -Content ("$original# BEGIN platform-ai meeting-ai private gateway test shim`r`n")
    Assert-ThrowsLike { Invoke-Shim } "unbalanced"

    New-Fixture -Content $original
    $mutexReadyPath = Join-Path $fixtureRoot "mutex-ready"
    $mutexJob = Start-Job -ArgumentList $mutexReadyPath -ScriptBlock {
        param($readyPath)
        $heldMutex = New-Object Threading.Mutex(
            $false,
            "Global\platform-ai-private-gateway-host-v1"
        )
        $held = $false
        try {
            $held = $heldMutex.WaitOne([TimeSpan]::FromSeconds(5))
            if (-not $held) { throw "Background contract could not acquire mutex." }
            [IO.File]::WriteAllText($readyPath, "ready")
            Start-Sleep -Seconds 30
        } finally {
            if ($held) { [void]$heldMutex.ReleaseMutex() }
            $heldMutex.Dispose()
        }
    }
    try {
        $mutexDeadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-Path -LiteralPath $mutexReadyPath) -and
            [DateTime]::UtcNow -lt $mutexDeadline -and
            $mutexJob.State -ne "Failed") {
            Start-Sleep -Milliseconds 100
        }
        if ($mutexJob.State -eq "Failed") {
            throw ("Background mutex contract failed: {0}" -f
                ($mutexJob | Receive-Job -ErrorAction SilentlyContinue | Out-String))
        }
        Assert-True (Test-Path -LiteralPath $mutexReadyPath) `
            "Background contract did not signal mutex ownership."
        Assert-ThrowsLike { Invoke-Shim -MutexTimeoutSeconds 0 } "Timed out waiting"
    } finally {
        Stop-Job -Job $mutexJob -ErrorAction SilentlyContinue
        Remove-Job -Job $mutexJob -Force -ErrorAction SilentlyContinue
    }

    Write-Host "private gateway hosts shim Windows contract: PASS"
} finally {
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
}
