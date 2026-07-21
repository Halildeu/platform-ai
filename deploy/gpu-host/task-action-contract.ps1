# Exact Windows Scheduled Task action contract for the GPU-host services.
# This file is dot-sourced by install.ps1 and migrate-task-actions.ps1.

Set-StrictMode -Version 2.0

if (-not ("PlatformAi.NativeCommandLine" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace PlatformAi {
    public static class NativeCommandLine {
        [DllImport("shell32.dll", SetLastError = true)]
        private static extern IntPtr CommandLineToArgvW(
            [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
            out int argumentCount
        );

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr pointer);

        public static string[] Split(string commandLine) {
            int count;
            IntPtr argv = CommandLineToArgvW(commandLine, out count);
            if (argv == IntPtr.Zero) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            try {
                string[] result = new string[count];
                for (int index = 0; index < count; index++) {
                    IntPtr value = Marshal.ReadIntPtr(argv, index * IntPtr.Size);
                    result[index] = Marshal.PtrToStringUni(value);
                }
                return result;
            } finally {
                LocalFree(argv);
            }
        }
    }
}
"@
}

function Test-GpuHostValueSafe {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [switch]$AllowSemicolonList
    )

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '[\x00-\x1f"]') {
        return $false
    }
    $values = @($Value)
    if ($AllowSemicolonList) {
        $values = @($Value.Split(';', [StringSplitOptions]::RemoveEmptyEntries))
        if ($values.Count -eq 0) { return $false }
    }
    foreach ($item in $values) {
        if ($item -notmatch '^[A-Za-z]:\\' -or $item.StartsWith('\\')) {
            return $false
        }
    }
    return $true
}

function ConvertTo-GpuHostWindowsArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value -notmatch '[\s"]' -and -not $Value.EndsWith('\')) {
        return $Value
    }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ([int]$character -eq 92) {
            $backslashes += 1
            continue
        }
        if ([int]$character -eq 34) {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
        } else {
            if ($backslashes -gt 0) {
                [void]$builder.Append(('\' * $backslashes))
            }
            [void]$builder.Append($character)
        }
        $backslashes = 0
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-GpuHostWindowsPowerShellPath {
    $systemRoot = $env:SystemRoot
    if ([string]::IsNullOrWhiteSpace($systemRoot)) {
        $systemRoot = "C:\Windows"
    }
    return Join-Path $systemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
}

function Get-GpuHostTaskSpec {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    switch ($TaskName) {
        'platform-ai-live-stt' {
            return [ordered]@{
                Script = 'start-live-stt.ps1'
                LiveStt = $true
                Port = 8200
            }
        }
        'platform-ai-meeting-ai' {
            return [ordered]@{
                Script = 'start-meeting-ai.ps1'
                LiveStt = $false
                Port = 8300
            }
        }
        default { throw "Unsupported GPU-host task name." }
    }
}

function New-GpuHostTaskActionArguments {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [string]$HfHome = '',
        [string]$CudaBin = ''
    )

    $spec = Get-GpuHostTaskSpec -TaskName $TaskName
    if (-not (Test-GpuHostValueSafe -Value $RepoRoot)) {
        throw 'RepoRoot is not a safe absolute local path.'
    }
    if (-not (Test-GpuHostValueSafe -Value $PythonExe)) {
        throw 'PythonExe is not a safe absolute local path.'
    }
    if ($spec.LiveStt -and -not (Test-GpuHostValueSafe -Value $HfHome)) {
        throw 'HfHome is required for the live STT task.'
    }
    if (-not $spec.LiveStt -and (-not [string]::IsNullOrWhiteSpace($HfHome) -or
        -not [string]::IsNullOrWhiteSpace($CudaBin))) {
        throw 'Meeting AI does not accept live STT-only action parameters.'
    }
    if (-not [string]::IsNullOrWhiteSpace($CudaBin) -and
        -not (Test-GpuHostValueSafe -Value $CudaBin -AllowSemicolonList)) {
        throw 'CudaBin is not a safe absolute local path list.'
    }

    $scriptPath = Join-Path (Join-Path $RepoRoot 'deploy\gpu-host') $spec.Script
    $tokens = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $scriptPath,
        '-RepoRoot', $RepoRoot,
        '-PythonExe', $PythonExe
    )
    if ($spec.LiveStt) {
        $tokens += @('-HfHome', $HfHome)
        if (-not [string]::IsNullOrWhiteSpace($CudaBin)) {
            $tokens += @('-CudaBin', $CudaBin)
        }
    }
    return (($tokens | ForEach-Object {
        ConvertTo-GpuHostWindowsArgument -Value $_
    }) -join ' ')
}

function Get-GpuHostTaskActionContract {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Execute,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [string]$WorkingDirectory = '',
        [switch]$AllowBarePowerShell
    )

    $result = [ordered]@{
        Valid = $false
        RepoClass = 'other'
        CanonicalArguments = ''
        PythonExe = ''
        HfHome = ''
        CudaBin = ''
    }
    try {
        $spec = Get-GpuHostTaskSpec -TaskName $TaskName
        $trustedPowerShell = Get-GpuHostWindowsPowerShellPath
        $absolutePowerShell = $trustedPowerShell.Equals(
            $Execute,
            [StringComparison]::OrdinalIgnoreCase
        )
        if (-not $absolutePowerShell -and
            (-not $AllowBarePowerShell -or $Execute -ine "powershell.exe")) {
            return $result
        }
        if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) { return $result }

        $tokens = @([PlatformAi.NativeCommandLine]::Split(
            ('powershell.exe {0}' -f $Arguments)
        ))
        $minimum = 10
        if ($spec.LiveStt) { $minimum = 12 }
        if ($tokens.Count -lt $minimum) { return $result }
        if ($tokens[0] -ine 'powershell.exe' -or
            $tokens[1] -ine '-NoProfile' -or
            $tokens[2] -ine '-ExecutionPolicy' -or
            $tokens[3] -ine 'Bypass' -or
            $tokens[4] -ine '-File' -or
            $tokens[6] -ine '-RepoRoot' -or
            $tokens[8] -ine '-PythonExe') {
            return $result
        }

        $canonicalRoot = 'C:\platform-ai'
        $legacyRoot = 'C:\Users\denetimpc\platform-ai'
        $canonicalScript = Join-Path (Join-Path $canonicalRoot 'deploy\gpu-host') $spec.Script
        $legacyScript = Join-Path (Join-Path $legacyRoot 'deploy\gpu-host') $spec.Script
        if ($tokens[5] -ieq $canonicalScript -and $tokens[7] -ieq $canonicalRoot) {
            $result.RepoClass = 'canonical-repo'
        } elseif ($tokens[5] -ieq $legacyScript -and $tokens[7] -ieq $legacyRoot) {
            $result.RepoClass = 'legacy-user-repo'
        } else {
            return $result
        }

        $pythonExe = $tokens[9]
        if (-not (Test-GpuHostValueSafe -Value $pythonExe)) { return $result }
        $hfHome = ''
        $cudaBin = ''
        $index = 10
        if ($spec.LiveStt) {
            if ($tokens[$index] -ine '-HfHome') { return $result }
            $hfHome = $tokens[$index + 1]
            if (-not (Test-GpuHostValueSafe -Value $hfHome)) { return $result }
            $index += 2
            if ($index -lt $tokens.Count) {
                if ($tokens[$index] -ine '-CudaBin' -or $index + 1 -ge $tokens.Count) {
                    return $result
                }
                $cudaBin = $tokens[$index + 1]
                if (-not (Test-GpuHostValueSafe -Value $cudaBin -AllowSemicolonList)) {
                    return $result
                }
                $index += 2
            }
        }
        if ($index -ne $tokens.Count) { return $result }

        $result.CanonicalArguments = New-GpuHostTaskActionArguments `
            -TaskName $TaskName -RepoRoot $canonicalRoot -PythonExe $pythonExe `
            -HfHome $hfHome -CudaBin $cudaBin
        $result.PythonExe = $pythonExe
        $result.HfHome = $hfHome
        $result.CudaBin = $cudaBin
        $result.Valid = $true
        return $result
    } catch {
        return $result
    }
}
