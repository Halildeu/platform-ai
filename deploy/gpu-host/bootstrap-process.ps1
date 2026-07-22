# Bounded child-process contract shared by fresh bootstrap and model verification.
# The caller must dot-source task-action-contract.ps1 first for argument quoting.

Set-StrictMode -Version 2.0

function Stop-GpuHostProcessTreeBounded {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
        [ValidateRange(1, 60)][int]$GraceSec = 10
    )

    if ($Process.HasExited) { return }
    $taskkill = New-Object Diagnostics.Process
    try {
        $killInfo = New-Object Diagnostics.ProcessStartInfo
        $killInfo.FileName = Join-Path $env:SystemRoot "System32\taskkill.exe"
        $killInfo.Arguments = "/PID {0} /T /F" -f $Process.Id
        $killInfo.UseShellExecute = $false
        $killInfo.CreateNoWindow = $true
        $killInfo.RedirectStandardOutput = $true
        $killInfo.RedirectStandardError = $true
        $taskkill.StartInfo = $killInfo
        if ($taskkill.Start()) {
            if (-not $taskkill.WaitForExit($GraceSec * 1000)) {
                try { $taskkill.Kill() } catch { }
                [void]$taskkill.WaitForExit(1000)
            }
        }
    } finally {
        $taskkill.Dispose()
    }

    if (-not $Process.WaitForExit($GraceSec * 1000)) {
        try { $Process.Kill() } catch { }
        if (-not $Process.WaitForExit($GraceSec * 1000)) {
            throw "Timed-out child process tree could not be terminated within the bounded grace period."
        }
    }
}

function Invoke-GpuHostBoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [ValidateRange(1, 86400)][int]$TimeoutSec,
        [ValidateRange(1, 60)][int]$KillGraceSec = 10,
        [string]$Operation = "GPU-host child operation"
    )

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FileName
    $startInfo.Arguments = (($ArgumentList | ForEach-Object {
        ConvertTo-GpuHostWindowsArgument -Value ([string]$_)
    }) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "$Operation could not be started."
        }
        $processId = $process.Id
        if (-not $process.WaitForExit($TimeoutSec * 1000)) {
            Stop-GpuHostProcessTreeBounded -Process $process -GraceSec $KillGraceSec
            throw "$Operation timed out after $TimeoutSec seconds; its process tree was terminated."
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            ProcessId = $processId
            TimedOut = $false
        }
    } finally {
        $process.Dispose()
    }
}

function Invoke-GpuHostBoundedWindowsPowerShellFile {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string[]]$ScriptArguments = @(),
        [ValidateRange(1, 86400)][int]$TimeoutSec,
        [ValidateRange(1, 60)][int]$KillGraceSec = 10,
        [string]$Operation = "Windows PowerShell child operation"
    )

    $tokens = @(
        "-NonInteractive",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $ScriptPath
    ) + @($ScriptArguments)
    return Invoke-GpuHostBoundedProcess `
        -FileName (Get-GpuHostWindowsPowerShellPath) `
        -ArgumentList $tokens -TimeoutSec $TimeoutSec `
        -KillGraceSec $KillGraceSec -Operation $Operation
}
