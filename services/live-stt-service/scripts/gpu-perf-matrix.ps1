[CmdletBinding()]
param(
    [string]$Image = "live-stt-service:gpu-issue-41",
    [string[]]$Models = @("medium", "large-v3"),
    [ValidateSet("float16", "int8_float16")]
    [string]$ComputeType = "float16",
    [string]$FixturesDir = "tests/fixtures",
    [string]$Pattern = "sample-tr-cv17-*.wav",
    [int]$BasePort = 18250,
    [int]$Timeout = 300
)

# #43 performance/accuracy matrix harness.
# For each model it starts the GPU image with STT_MODEL_NAME=<model>, transcribes
# the labelled fixture set (wav + .txt reference), and records corpus WER,
# latency per audio-minute, real-time factor and peak VRAM. MEASUREMENT ONLY —
# no production model is selected here; that decision is read off the matrix.

$ErrorActionPreference = "Stop"
$serviceRoot = Split-Path -Parent $PSScriptRoot
$fixturesPath = (Resolve-Path (Join-Path $serviceRoot $FixturesDir)).Path
$cachePath = Join-Path $env:USERPROFILE ".cache\huggingface"
$client = Join-Path $PSScriptRoot "perf_client.py"
$docker = "docker"
$reqTimeout = [Math]::Min($Timeout, 300)
$rows = @()
$index = 0

New-Item -ItemType Directory -Force -Path $cachePath | Out-Null

function Get-VramUsedMiB {
    $val = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits |
        Select-Object -First 1
    return [int]($val.Trim())
}

function Wait-Healthy {
    param([int]$Port)
    for ($i = 1; $i -le 90; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($h) { return $true }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

foreach ($model in $Models) {
    $index++
    $port = $BasePort + $index
    $safe = $model -replace "[^a-zA-Z0-9_.-]", "-"
    $name = "live-stt-perf-$PID-$safe"
    Write-Host "=== Model=$model Port=$port ==="

    $runArgs = @(
        "run", "-d", "--rm", "--name", $name, "--gpus", "all",
        "-p", "${port}:8200",
        "--mount", "type=bind,source=$cachePath,target=/home/stt/.cache/huggingface",
        "-e", "STT_MODEL_NAME=$model",
        "-e", "STT_LANGUAGE=tr",
        "-e", "STT_DEVICE=cuda",
        "-e", "STT_COMPUTE_TYPE=$ComputeType",
        "-e", "STT_BEAM_SIZE=1",
        "-e", "STT_REQUEST_TIMEOUT=$reqTimeout",
        $Image
    )

    try {
        & $docker @runArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "docker run failed ($model)" }
        if (-not (Wait-Healthy -Port $port)) {
            & $docker logs $name
            throw "health endpoint not ready ($model)"
        }

        # Warm up so the (possibly first-time) model download/load is excluded
        # from the timed corpus run.
        & python $client --url "http://127.0.0.1:$port/transcribe" `
            --fixtures-dir $fixturesPath --pattern $Pattern --timeout $Timeout | Out-Null

        $baselineVram = Get-VramUsedMiB
        $peakFile = Join-Path $env:TEMP "stt-perf-peak-$PID-$safe.txt"
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
            --fixtures-dir $fixturesPath --pattern $Pattern --timeout $Timeout | Out-String

        Stop-Job $sampler -ErrorAction SilentlyContinue | Out-Null
        Remove-Job $sampler -Force -ErrorAction SilentlyContinue | Out-Null
        $peakVram = [int]((Get-Content $peakFile | Select-Object -First 1).Trim())
        Remove-Item $peakFile -ErrorAction SilentlyContinue

        $s = ($clientOut | ConvertFrom-Json).summary
        $rows += [pscustomobject]@{
            Model              = $model
            Ok                 = $s.ok
            Errors             = $s.errors
            CorpusWER          = $s.corpus_wer
            RefWords           = $s.ref_words
            LatencyMsPerAudMin = $s.latency_ms_per_audio_min
            RealtimeFactor     = $s.realtime_factor
            PeakVramMiB        = $peakVram
        }
    } finally {
        & $docker rm -f $name 2>$null | Out-Null
    }
}

Write-Host ""
Write-Host "=== #43 PERFORMANCE / ACCURACY MATRIX ==="
$rows | Format-Table -AutoSize
Write-Host "perf matrix DONE"
