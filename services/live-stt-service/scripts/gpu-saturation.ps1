[CmdletBinding()]
param(
    [string]$Image = "live-stt-service:gpu-issue-41",
    [string]$Model = "medium",
    [ValidateSet("float16", "int8_float16")]
    [string]$ComputeType = "float16",
    [string]$Fixture = "tests/fixtures/sample-tr-cv17-001.wav",
    [int[]]$Workers = @(1, 2, 3, 4),
    [int]$Concurrency = 0,           # 0 => concurrency equals worker count K
    [int]$BasePort = 18230,
    [int]$Timeout = 300
)

# #42 GPU saturation harness.
# For each worker count K it starts the GPU image with STT_WORKER_MAX_WORKERS=K,
# warms the model, fires K (or -Concurrency) parallel /transcribe requests while
# sampling VRAM, and records throughput / p50 / p95 / overlap / errors / VRAM.
# MEASUREMENT ONLY — no runtime defaults are changed, nothing is auto-clamped.

$ErrorActionPreference = "Stop"
$serviceRoot = Split-Path -Parent $PSScriptRoot
$fixturePath = (Resolve-Path (Join-Path $serviceRoot $Fixture)).Path
$cachePath = Join-Path $env:USERPROFILE ".cache\huggingface"
$client = Join-Path $PSScriptRoot "saturation_client.py"
$docker = "docker"
$rows = @()

# STT_REQUEST_TIMEOUT is validated server-side as <= 300 (config.py le=300).
# The client HTTP timeout may stay larger; only the container env is clamped.
$reqTimeout = [Math]::Min($Timeout, 300)

New-Item -ItemType Directory -Force -Path $cachePath | Out-Null

function Get-VramUsedMiB {
    $val = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits |
        Select-Object -First 1
    return [int]($val.Trim())
}

function Wait-Healthy {
    param([int]$Port)
    for ($i = 1; $i -le 60; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($h) { return $true }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

foreach ($k in $Workers) {
    $conc = if ($Concurrency -gt 0) { $Concurrency } else { $k }
    $port = $BasePort + $k
    $name = "live-stt-sat-$PID-$k"
    Write-Host "=== Workers=$k Concurrency=$conc Port=$port ==="

    # docker invoked directly with an explicit arg array (PowerShell advanced
    # functions mis-parse -d/-e — see #41 fix f175ab1).
    $runArgs = @(
        "run", "-d", "--rm", "--name", $name, "--gpus", "all",
        "-p", "${port}:8200",
        "--mount", "type=bind,source=$cachePath,target=/home/stt/.cache/huggingface",
        "-e", "STT_MODEL_NAME=$Model",
        "-e", "STT_LANGUAGE=tr",
        "-e", "STT_DEVICE=cuda",
        "-e", "STT_COMPUTE_TYPE=$ComputeType",
        "-e", "STT_BEAM_SIZE=1",
        "-e", "STT_REQUEST_TIMEOUT=$reqTimeout",
        "-e", "STT_WORKER_MAX_WORKERS=$k",
        $Image
    )

    try {
        & $docker @runArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "docker run failed (K=$k)" }

        if (-not (Wait-Healthy -Port $port)) {
            & $docker logs $name
            throw "health endpoint not ready (K=$k)"
        }

        # Warm up every worker slot so model-load time is excluded from the
        # measured run (fire K sequential priming requests).
        for ($w = 0; $w -lt $k; $w++) {
            & python $client --url "http://127.0.0.1:$port/transcribe" `
                --audio $fixturePath --concurrency 1 --timeout $Timeout | Out-Null
        }

        $baselineVram = Get-VramUsedMiB

        # Background VRAM peak sampler during the concurrent load.
        $peakFile = Join-Path $env:TEMP "stt-sat-peak-$PID-$k.txt"
        Set-Content -Path $peakFile -Value $baselineVram
        $sampler = Start-Job -ScriptBlock {
            param($outFile, $seed)
            $peak = [int]$seed
            while ($true) {
                $u = (& nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits |
                    Select-Object -First 1)
                $u = [int]($u.Trim())
                if ($u -gt $peak) { $peak = $u; Set-Content -Path $outFile -Value $peak }
                Start-Sleep -Milliseconds 150
            }
        } -ArgumentList $peakFile, $baselineVram

        $clientOut = & python $client --url "http://127.0.0.1:$port/transcribe" `
            --audio $fixturePath --concurrency $conc --timeout $Timeout | Out-String

        Stop-Job $sampler -ErrorAction SilentlyContinue | Out-Null
        Remove-Job $sampler -Force -ErrorAction SilentlyContinue | Out-Null
        $peakVram = [int]((Get-Content $peakFile | Select-Object -First 1).Trim())
        Remove-Item $peakFile -ErrorAction SilentlyContinue

        $parsed = $clientOut | ConvertFrom-Json
        $s = $parsed.summary

        $rows += [pscustomobject]@{
            Workers        = $k
            Concurrency    = $conc
            Ok             = $s.ok
            Errors         = $s.errors
            P50ms          = $s.p50_ms
            P95ms          = $s.p95_ms
            ThroughputRps  = $s.throughput_rps
            Overlap        = $s.overlap
            MaxConcurrency = $s.max_concurrency
            VramBaseMiB    = $baselineVram
            VramPeakMiB    = $peakVram
            VramDeltaMiB   = $peakVram - $baselineVram
        }
    } finally {
        & $docker rm -f $name 2>$null | Out-Null
    }
}

Write-Host ""
Write-Host "=== #42 SATURATION RESULTS ==="
$rows | Format-Table -AutoSize
Write-Host "saturation harness DONE"
