# Fail-closed GPU-host listener restart and process-identity acceptance.
# Windows PowerShell 5.1 compatible. Dot-source task-action-contract.ps1 first.

Set-StrictMode -Version 2.0

function New-GpuHostOwnerResult {
    param([bool]$Succeeded, [int[]]$Owners = @(), [string]$Reason = "")
    return [pscustomobject]@{
        Succeeded = $Succeeded
        Owners = @($Owners | Sort-Object -Unique)
        Reason = $Reason
    }
}

function Get-GpuHostListeningPortOwnerSnapshot {
    param([Parameter(Mandatory = $true)][int]$Port)

    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = @(& netstat.exe -ano -p TCP 2> $null)
        if ($LASTEXITCODE -ne 0) {
            return New-GpuHostOwnerResult -Succeeded $false -Reason "query-failed"
        }
        $pattern = '^\s*TCP\s+\S+:' + $Port + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
        $candidate = '^\s*TCP\s+\S+:' + $Port + '\s+'
        $owners = @()
        foreach ($line in $lines) {
            if ([string]$line -match $pattern) {
                $owners += [int]$Matches[1]
            } elseif ([string]$line -match $candidate -and
                [string]$line -match '(?i)LISTEN') {
                return New-GpuHostOwnerResult -Succeeded $false -Reason "parse-failed"
            }
        }
        return New-GpuHostOwnerResult -Succeeded $true -Owners $owners
    } catch {
        return New-GpuHostOwnerResult -Succeeded $false -Reason "query-exception"
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Invoke-GpuHostOwnerQuery {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$OwnerQuery,
        [Parameter(Mandatory = $true)][int]$Port
    )

    try {
        $result = & $OwnerQuery $Port
        if ($null -eq $result -or
            $result.PSObject.Properties.Name -notcontains "Succeeded" -or
            $result.PSObject.Properties.Name -notcontains "Owners") {
            return New-GpuHostOwnerResult -Succeeded $false -Reason "invalid-result"
        }
        return New-GpuHostOwnerResult -Succeeded ([bool]$result.Succeeded) `
            -Owners @($result.Owners) -Reason ([string]$result.Reason)
    } catch {
        return New-GpuHostOwnerResult -Succeeded $false -Reason "query-exception"
    }
}

function Test-GpuHostDeadlineOpen {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec
    )
    return $Clock.Elapsed.TotalSeconds -lt $DeadlineSec
}

function Wait-GpuHostPortReleased {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec,
        [scriptblock]$OwnerQuery = { param($value) Get-GpuHostListeningPortOwnerSnapshot -Port $value },
        [int]$StableSamples = 2
    )

    $stable = 0
    while (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec) {
        $snapshot = Invoke-GpuHostOwnerQuery -OwnerQuery $OwnerQuery -Port $Port
        if (-not $snapshot.Succeeded) { return $snapshot }
        if (@($snapshot.Owners).Count -eq 0) {
            $stable++
            if ($stable -ge $StableSamples) { return $snapshot }
        } else {
            $stable = 0
        }
        Start-Sleep -Milliseconds 250
    }
    return New-GpuHostOwnerResult -Succeeded $false -Reason "deadline-exhausted"
}

function Wait-GpuHostNewPortOwner {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int[]]$PreviousOwners = @(),
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec,
        [scriptblock]$OwnerQuery = { param($value) Get-GpuHostListeningPortOwnerSnapshot -Port $value },
        [int]$StableSamples = 3
    )

    $stable = 0
    $candidate = ""
    while (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec) {
        $snapshot = Invoke-GpuHostOwnerQuery -OwnerQuery $OwnerQuery -Port $Port
        if (-not $snapshot.Succeeded) { return $snapshot }
        $owners = @($snapshot.Owners)
        $newOwners = @($owners | Where-Object { $PreviousOwners -notcontains $_ })
        if ($owners.Count -eq 1 -and $newOwners.Count -eq 1) {
            $current = "$($newOwners[0])"
            if ($current -eq $candidate) { $stable++ } else { $candidate = $current; $stable = 1 }
            if ($stable -ge $StableSamples) {
                return New-GpuHostOwnerResult -Succeeded $true -Owners $newOwners
            }
        } else {
            $candidate = ""
            $stable = 0
        }
        Start-Sleep -Milliseconds 250
    }
    return New-GpuHostOwnerResult -Succeeded $false -Reason "deadline-exhausted"
}

function Get-GpuHostTaskInstancePids {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec
    )

    try {
        $service = New-Object -ComObject "Schedule.Service"
        $service.Connect()
        $task = $service.GetFolder("\").GetTask($TaskName)
        do {
            $pids = @($task.GetInstances(0) | ForEach-Object { [int]$_.EnginePID })
            if ($pids.Count -gt 0) { return @($pids | Sort-Object -Unique) }
            Start-Sleep -Milliseconds 250
        } while (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec)
    } catch { }
    return @()
}

function Get-GpuHostStringSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($Value)
        ))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-GpuHostListenerIdentityProof {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedPythonExe,
        [Parameter(Mandatory = $true)][int]$ExpectedPort,
        [Parameter(Mandatory = $true)][int[]]$ExpectedTaskPids,
        [scriptblock]$ProcessQuery = {
            param($value)
            Get-WmiObject -Class Win32_Process -Filter ("ProcessId = {0}" -f $value) `
                -ErrorAction Stop
        }
    )

    try {
        $listener = & $ProcessQuery $ProcessId
        if ($null -eq $listener -or
            [string]::IsNullOrWhiteSpace([string]$listener.ExecutablePath) -or
            [string]::IsNullOrWhiteSpace([string]$listener.CommandLine) -or
            $null -eq $listener.CreationDate) {
            throw "invalid"
        }
        $expectedExe = [IO.Path]::GetFullPath($ExpectedPythonExe)
        $actualExe = [IO.Path]::GetFullPath([string]$listener.ExecutablePath)
        if (-not $actualExe.Equals($expectedExe, [StringComparison]::OrdinalIgnoreCase)) {
            throw "interpreter-mismatch"
        }
        $tokens = @([PlatformAi.NativeCommandLine]::Split([string]$listener.CommandLine))
        $moduleOk = $false
        $portOk = $false
        for ($index = 0; $index -lt $tokens.Count; $index++) {
            if ($tokens[$index] -ieq "-m" -and $index + 1 -lt $tokens.Count -and
                $tokens[$index + 1] -ieq "uvicorn") { $moduleOk = $true }
            if ($tokens[$index] -ieq "--port" -and $index + 1 -lt $tokens.Count -and
                $tokens[$index + 1] -eq "$ExpectedPort") { $portOk = $true }
        }
        if (-not $moduleOk -or -not $portOk) { throw "command-mismatch" }

        $chain = @()
        $currentId = $ProcessId
        $ancestorMatched = $false
        for ($depth = 0; $depth -lt 8 -and $currentId -gt 0; $depth++) {
            $process = & $ProcessQuery $currentId
            if ($null -eq $process -or $null -eq $process.CreationDate) { throw "invalid-chain" }
            $parentId = [int]$process.ParentProcessId
            $chain += "{0}:{1}:{2}" -f $currentId, $parentId, ([string]$process.CreationDate)
            if ($ExpectedTaskPids -contains $currentId) { $ancestorMatched = $true; break }
            if ($parentId -le 4 -or $parentId -eq $currentId) { break }
            $currentId = $parentId
        }
        if (-not $ancestorMatched) { throw "task-ancestry-mismatch" }

        $identity = "{0}|{1}|{2}|{3}" -f $ProcessId, $actualExe.ToLowerInvariant(), `
            ([string]$listener.CreationDate), ($chain -join ";")
        return [pscustomobject]@{
            Succeeded = $true
            ProcessId = $ProcessId
            IdentitySha256 = Get-GpuHostStringSha256 -Value $identity
            Reason = ""
        }
    } catch {
        return [pscustomobject]@{
            Succeeded = $false
            ProcessId = $ProcessId
            IdentitySha256 = ""
            Reason = "process-contract-failed"
        }
    }
}

function Test-GpuHostListenerStable {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ExpectedOwnerId,
        [Parameter(Mandatory = $true)][string]$ExpectedPythonExe,
        [Parameter(Mandatory = $true)][int[]]$ExpectedTaskPids,
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec,
        [scriptblock]$OwnerQuery = { param($value) Get-GpuHostListeningPortOwnerSnapshot -Port $value },
        [scriptblock]$ProcessProofQuery = $null,
        [int]$StableSamples = 3
    )

    $expectedHash = ""
    for ($sample = 0; $sample -lt $StableSamples; $sample++) {
        if (-not (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec)) {
            return $false
        }
        $snapshot = Invoke-GpuHostOwnerQuery -OwnerQuery $OwnerQuery -Port $Port
        if (-not $snapshot.Succeeded -or @($snapshot.Owners).Count -ne 1 -or
            [int]$snapshot.Owners[0] -ne $ExpectedOwnerId) { return $false }
        if ($null -eq $ProcessProofQuery) {
            $proof = Get-GpuHostListenerIdentityProof -ProcessId $ExpectedOwnerId `
                -ExpectedPythonExe $ExpectedPythonExe -ExpectedPort $Port `
                -ExpectedTaskPids $ExpectedTaskPids
        } else {
            $proof = & $ProcessProofQuery $ExpectedOwnerId $ExpectedPythonExe $Port $ExpectedTaskPids
        }
        if ($null -eq $proof -or -not [bool]$proof.Succeeded -or
            [string]::IsNullOrWhiteSpace([string]$proof.IdentitySha256)) { return $false }
        if ([string]::IsNullOrWhiteSpace($expectedHash)) {
            $expectedHash = [string]$proof.IdentitySha256
        } elseif ($expectedHash -ne [string]$proof.IdentitySha256) {
            return $false
        }
        if ($sample + 1 -lt $StableSamples) { Start-Sleep -Milliseconds 500 }
    }
    return $true
}
