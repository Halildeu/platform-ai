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

function ConvertFrom-GpuHostNetstatLines {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [AllowEmptyCollection()][string[]]$Lines = @()
    )

    if ($ExitCode -ne 0) {
        return New-GpuHostOwnerResult -Succeeded $false -Reason "query-failed"
    }
    $pattern = '^\s*TCP\s+\S+:' + $Port + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
    $candidate = '^\s*TCP\s+\S+:' + $Port + '\s+'
    $owners = @()
    foreach ($line in $Lines) {
        if ([string]$line -match $pattern) {
            $owners += [int]$Matches[1]
        } elseif ([string]$line -match $candidate -and
            [string]$line -match '(?i)LISTEN') {
            return New-GpuHostOwnerResult -Succeeded $false -Reason "parse-failed"
        }
    }
    return New-GpuHostOwnerResult -Succeeded $true -Owners $owners
}

function Get-GpuHostListeningPortOwnerSnapshot {
    param([Parameter(Mandatory = $true)][int]$Port)

    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = @(& netstat.exe -ano -p TCP 2> $null)
        return ConvertFrom-GpuHostNetstatLines -Port $Port `
            -ExitCode $LASTEXITCODE -Lines $lines
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

function Test-MeetingAiDependencyReadiness {
    param([AllowNull()]$Readiness)

    if ($null -eq $Readiness) { return $false }
    $topLevel = @($Readiness.PSObject.Properties | ForEach-Object { $_.Name })
    if ($topLevel -notcontains "analysis_delivery" -or
        $topLevel -notcontains "ready_consumer" -or
        $null -eq $Readiness.analysis_delivery -or
        $null -eq $Readiness.ready_consumer) {
        return $false
    }
    $deliveryFields = @(
        $Readiness.analysis_delivery.PSObject.Properties | ForEach-Object { $_.Name }
    )
    if ($deliveryFields -notcontains "ready" -or
        $Readiness.analysis_delivery.ready -ne $true) {
        return $false
    }
    $consumerFields = @(
        $Readiness.ready_consumer.PSObject.Properties | ForEach-Object { $_.Name }
    )
    if ($consumerFields -notcontains "enabled" -or
        $consumerFields -notcontains "ready" -or
        $Readiness.ready_consumer.ready -ne $true) {
        return $false
    }
    if ($Readiness.ready_consumer.enabled -eq $false) { return $true }
    if ($Readiness.ready_consumer.enabled -ne $true) { return $false }
    foreach ($required in @("worker_running", "redis_group_ready", "error_code")) {
        if ($consumerFields -notcontains $required) { return $false }
    }
    return (
        $Readiness.ready_consumer.worker_running -eq $true -and
        $Readiness.ready_consumer.redis_group_ready -eq $true -and
        $null -eq $Readiness.ready_consumer.error_code
    )
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

function New-GpuHostTaskInstanceResult {
    param([bool]$Succeeded, [object[]]$Instances = @(), [string]$Reason = "")
    return [pscustomobject]@{
        Succeeded = $Succeeded
        Instances = @($Instances)
        Reason = $Reason
    }
}

function Get-GpuHostTaskInstanceSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName
    )

    try {
        $service = New-Object -ComObject "Schedule.Service"
        $service.Connect()
        $task = $service.GetFolder("\").GetTask($TaskName)
        $instances = @($task.GetInstances(0) | ForEach-Object {
            [pscustomobject]@{
                InstanceGuid = ([string]$_.InstanceGuid).ToLowerInvariant()
                EnginePid = [int]$_.EnginePID
            }
        })
        foreach ($instance in $instances) {
            if ($instance.InstanceGuid -notmatch '^\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?$' -or
                $instance.EnginePid -le 0) {
                return New-GpuHostTaskInstanceResult -Succeeded $false `
                    -Reason "invalid-instance"
            }
        }
        return New-GpuHostTaskInstanceResult -Succeeded $true -Instances $instances
    } catch {
        return New-GpuHostTaskInstanceResult -Succeeded $false -Reason "query-failed"
    }
}

function Invoke-GpuHostTaskInstanceQuery {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$TaskInstanceQuery,
        [Parameter(Mandatory = $true)][string]$TaskName
    )
    try {
        $result = & $TaskInstanceQuery $TaskName
        if ($null -eq $result -or
            $result.PSObject.Properties.Name -notcontains "Succeeded" -or
            $result.PSObject.Properties.Name -notcontains "Instances") {
            return New-GpuHostTaskInstanceResult -Succeeded $false -Reason "invalid-result"
        }
        return New-GpuHostTaskInstanceResult -Succeeded ([bool]$result.Succeeded) `
            -Instances @($result.Instances) -Reason ([string]$result.Reason)
    } catch {
        return New-GpuHostTaskInstanceResult -Succeeded $false -Reason "query-exception"
    }
}

function Wait-GpuHostTaskInstancesReleased {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string[]]$PreviousInstanceGuids = @(),
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec,
        [scriptblock]$TaskInstanceQuery = {
            param($value) Get-GpuHostTaskInstanceSnapshot -TaskName $value
        },
        [int]$StableSamples = 2
    )
    $stable = 0
    while (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec) {
        $snapshot = Invoke-GpuHostTaskInstanceQuery `
            -TaskInstanceQuery $TaskInstanceQuery -TaskName $TaskName
        if (-not $snapshot.Succeeded) { return $snapshot }
        $remaining = @($snapshot.Instances | Where-Object {
            $PreviousInstanceGuids -contains ([string]$_.InstanceGuid).ToLowerInvariant()
        })
        if ($remaining.Count -eq 0) {
            $stable++
            if ($stable -ge $StableSamples) { return $snapshot }
        } else {
            $stable = 0
        }
        Start-Sleep -Milliseconds 250
    }
    return New-GpuHostTaskInstanceResult -Succeeded $false -Reason "deadline-exhausted"
}

function Wait-GpuHostNewTaskInstance {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string[]]$PreviousInstanceGuids = @(),
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec,
        [scriptblock]$TaskInstanceQuery = {
            param($value) Get-GpuHostTaskInstanceSnapshot -TaskName $value
        },
        [int]$StableSamples = 3
    )
    $stable = 0
    $candidate = ""
    while (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec) {
        $snapshot = Invoke-GpuHostTaskInstanceQuery `
            -TaskInstanceQuery $TaskInstanceQuery -TaskName $TaskName
        if (-not $snapshot.Succeeded) { return $snapshot }
        $newInstances = @($snapshot.Instances | Where-Object {
            $PreviousInstanceGuids -notcontains ([string]$_.InstanceGuid).ToLowerInvariant()
        })
        if (@($snapshot.Instances).Count -eq 1 -and $newInstances.Count -eq 1) {
            $current = "{0}|{1}" -f `
                ([string]$newInstances[0].InstanceGuid).ToLowerInvariant(), `
                ([int]$newInstances[0].EnginePid)
            if ($current -eq $candidate) { $stable++ } else { $candidate = $current; $stable = 1 }
            if ($stable -ge $StableSamples) {
                return New-GpuHostTaskInstanceResult -Succeeded $true `
                    -Instances $newInstances
            }
        } else {
            $candidate = ""
            $stable = 0
        }
        Start-Sleep -Milliseconds 250
    }
    return New-GpuHostTaskInstanceResult -Succeeded $false -Reason "deadline-exhausted"
}

function Test-GpuHostTaskInstanceStable {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedInstanceGuid,
        [Parameter(Mandatory = $true)][int]$ExpectedEnginePid,
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Clock,
        [Parameter(Mandatory = $true)][double]$DeadlineSec,
        [scriptblock]$TaskInstanceQuery = {
            param($value) Get-GpuHostTaskInstanceSnapshot -TaskName $value
        },
        [int]$StableSamples = 2
    )
    for ($sample = 0; $sample -lt $StableSamples; $sample++) {
        if (-not (Test-GpuHostDeadlineOpen -Clock $Clock -DeadlineSec $DeadlineSec)) {
            return $false
        }
        $snapshot = Invoke-GpuHostTaskInstanceQuery `
            -TaskInstanceQuery $TaskInstanceQuery -TaskName $TaskName
        if (-not $snapshot.Succeeded -or @($snapshot.Instances).Count -ne 1) {
            return $false
        }
        $instance = $snapshot.Instances[0]
        if (([string]$instance.InstanceGuid).ToLowerInvariant() -ne `
                $ExpectedInstanceGuid.ToLowerInvariant() -or
            [int]$instance.EnginePid -ne $ExpectedEnginePid) {
            return $false
        }
        if ($sample + 1 -lt $StableSamples) { Start-Sleep -Milliseconds 250 }
    }
    return $true
}

function Get-GpuHostTaskXmlContract {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$TaskXml,
        [switch]$SkipPythonPathValidation
    )
    try {
        [xml]$document = $TaskXml
        $namespace = New-Object Xml.XmlNamespaceManager($document.NameTable)
        $namespace.AddNamespace("t", $document.DocumentElement.NamespaceURI)
        $principals = @($document.SelectNodes("//t:Principals/t:Principal", $namespace))
        $actions = @($document.SelectNodes("//t:Actions", $namespace))
        $actionChildren = @($document.SelectNodes("//t:Actions/*", $namespace))
        $execs = @($document.SelectNodes("//t:Actions/t:Exec", $namespace))
        if ($principals.Count -ne 1 -or $actions.Count -ne 1 -or
            $actionChildren.Count -ne 1 -or $execs.Count -ne 1) {
            throw "action-cardinality-invalid"
        }
        $principal = $principals[0]
        $principalId = [string]$principal.GetAttribute("id")
        $actionContext = [string]$actions[0].GetAttribute("Context")
        if ([string]::IsNullOrWhiteSpace($principalId) -or
            [string]::IsNullOrWhiteSpace($actionContext) -or
            -not $principalId.Equals($actionContext, [StringComparison]::Ordinal)) {
            throw "action-context-invalid"
        }
        # Read every principal child through SelectSingleNode rather than
        # property access. Task Scheduler omits elements that carry their
        # default value, and under Set-StrictMode a missing element makes
        # `$principal.LogonType` throw a raw PowerShell property error instead
        # of reaching the intended "principal-invalid" verdict — the deploy
        # then fails with an unreadable reason.
        #
        # LogonType is the case that actually occurs: Windows never writes it
        # for the LocalSystem SID, because LocalSystem has no interactive logon
        # and ServiceAccount is its only meaning. Requiring the element
        # literally rejected the exact principal install.ps1 creates.
        #
        # An absent element is only ever read as its documented default, so the
        # security intent is unchanged: a non-SYSTEM user, an explicit
        # non-ServiceAccount logon, or a missing/lesser RunLevel still fail.
        $principalValue = {
            param([string]$Name, [string]$Default)
            $node = $principal.SelectSingleNode("t:{0}" -f $Name, $namespace)
            if ($null -eq $node) { return $Default }
            return [string]$node.InnerText
        }
        $userId = & $principalValue "UserId" ""
        $isLocalSystem = $userId -in @("SYSTEM", "S-1-5-18")
        # Only LocalSystem may imply its logon type. For any other identity a
        # missing element is not an implied service logon and must fail.
        $logonDefault = if ($isLocalSystem) { "ServiceAccount" } else { "" }
        $logonType = & $principalValue "LogonType" $logonDefault
        $runLevel = & $principalValue "RunLevel" "LeastPrivilege"
        if (-not $isLocalSystem -or
            $logonType -ne "ServiceAccount" -or
            $runLevel -ne "HighestAvailable") {
            throw "principal-invalid"
        }
        $exec = $execs[0]
        $commandNode = $exec.SelectSingleNode("t:Command", $namespace)
        $argumentsNode = $exec.SelectSingleNode("t:Arguments", $namespace)
        if ($null -eq $commandNode -or $null -eq $argumentsNode) {
            throw "action-contract-invalid"
        }
        $workingDirectory = ""
        $workingDirectoryNode = $exec.SelectSingleNode("t:WorkingDirectory", $namespace)
        if ($null -ne $workingDirectoryNode) {
            $workingDirectory = [string]$workingDirectoryNode.InnerText
        }
        $actionContract = Get-GpuHostTaskActionContract -TaskName $TaskName `
            -Execute ([string]$commandNode.InnerText) `
            -Arguments ([string]$argumentsNode.InnerText) `
            -WorkingDirectory $workingDirectory
        if (-not $actionContract.Valid) {
            throw "action-contract-invalid"
        }
        if (-not $SkipPythonPathValidation -and
            -not (Test-Path -LiteralPath $actionContract.PythonExe -PathType Leaf)) {
            throw "python-path-invalid"
        }
        return [pscustomobject]@{
            Valid = $true
            PythonExe = [string]$actionContract.PythonExe
            RepoRoot = [string]$actionContract.RepoRoot
            Reason = ""
        }
    } catch {
        return [pscustomobject]@{
            Valid = $false
            PythonExe = ""
            RepoRoot = ""
            Reason = [string]$_.Exception.Message
        }
    }
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
        $expectedTokens = @(
            $expectedExe,
            "-m", "uvicorn", "app.main:app",
            "--host", "0.0.0.0",
            "--port", "$ExpectedPort"
        )
        if ($tokens.Count -ne $expectedTokens.Count) { throw "command-mismatch" }
        for ($index = 0; $index -lt $expectedTokens.Count; $index++) {
            if (-not $tokens[$index].Equals(
                $expectedTokens[$index],
                [StringComparison]::OrdinalIgnoreCase
            )) { throw "command-mismatch" }
        }

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
