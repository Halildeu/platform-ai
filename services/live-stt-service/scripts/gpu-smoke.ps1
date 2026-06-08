[CmdletBinding()]
param(
    [string]$Image = "live-stt-service:gpu-issue-41",
    [string]$Model = "medium",
    [ValidateSet("float16", "int8_float16")]
    [string]$ComputeType = "float16",
    [string]$Fixture = "tests/fixtures/sample-tr-cv17-001.wav",
    [int]$Port = 18220,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$serviceRoot = Split-Path -Parent $PSScriptRoot
$fixturePath = (Resolve-Path (Join-Path $serviceRoot $Fixture)).Path
$containerName = "live-stt-gpu-smoke-$PID"
$cachePath = Join-Path $env:USERPROFILE ".cache\huggingface"
$docker = "docker"

New-Item -ItemType Directory -Force -Path $cachePath | Out-Null

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker command failed: docker $($Arguments -join ' ')"
    }
}

try {
    if (-not $SkipBuild) {
        Invoke-Docker build -f (Join-Path $serviceRoot "Dockerfile.gpu") `
            -t $Image $serviceRoot
    }

    Write-Host "[1/6] NVIDIA runtime visibility"
    Invoke-Docker run --rm --gpus all --entrypoint nvidia-smi $Image `
        --query-gpu=name,driver_version,memory.total --format=csv,noheader

    Write-Host "[2/6] CTranslate2 CUDA capability"
    Invoke-Docker run --rm --gpus all --entrypoint python3 $Image -c `
        "import ctranslate2; assert ctranslate2.get_cuda_device_count() > 0; print('devices=', ctranslate2.get_cuda_device_count()); print('compute=', sorted(ctranslate2.get_supported_compute_types('cuda')))"

    Write-Host "[3/6] cuBLAS/cuDNN linkage"
    Invoke-Docker run --rm --gpus all --entrypoint bash $Image -lc `
        "ldconfig -p | grep -E 'libcublas.so.12|libcudnn.so.8'"

    Write-Host "[4/6] Optional FFmpeg GPU video capability"
    & $docker run --rm --gpus all --entrypoint bash $Image -lc `
        "echo 'hwaccels:'; ffmpeg -hide_banner -hwaccels 2>/dev/null | grep -E 'cuda' || true; echo 'nvdec:'; ffmpeg -hide_banner -decoders 2>/dev/null | grep -E 'cuvid' || true; echo 'nvenc:'; ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'h264_nvenc|hevc_nvenc' || true"
    if ($LASTEXITCODE -ne 0) {
        throw "NVENC capability probe failed"
    }

    Write-Host "[5/6] Service start and real GPU transcription"
    Invoke-Docker run -d --rm --name $containerName --gpus all `
        -p "${Port}:8200" `
        --mount "type=bind,source=$cachePath,target=/home/stt/.cache/huggingface" `
        -e "STT_MODEL_NAME=$Model" `
        -e "STT_LANGUAGE=tr" `
        -e "STT_DEVICE=cuda" `
        -e "STT_COMPUTE_TYPE=$ComputeType" `
        -e "STT_BEAM_SIZE=1" `
        -e "STT_REQUEST_TIMEOUT=300" `
        $Image

    $health = $null
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if ($null -eq $health) {
        Invoke-Docker logs $containerName
        throw "GPU service health endpoint did not become ready"
    }

    $started = Get-Date
    $responseJson = & curl.exe -sS --fail --max-time 360 `
        -X POST -F "audio=@$fixturePath;type=audio/wav" `
        "http://127.0.0.1:$Port/transcribe"
    if ($LASTEXITCODE -ne 0) {
        Invoke-Docker logs $containerName
        throw "GPU transcription request failed"
    }
    $wallMs = [int]((Get-Date) - $started).TotalMilliseconds
    $response = $responseJson | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($response.text)) {
        throw "GPU transcription returned empty text"
    }
    if ($response.device -ne "cuda" -or $response.compute_type -ne $ComputeType) {
        throw "Unexpected runtime metadata: device=$($response.device) compute=$($response.compute_type)"
    }

    Write-Host "[6/6] Image and runtime evidence"
    $imageSize = & $docker image inspect $Image --format "{{.Size}}"
    $runtimeUser = & $docker exec $containerName sh -c "id -u; id -g"
    $gpuMemory = & $docker exec $containerName nvidia-smi `
        --query-compute-apps=used_memory --format=csv,noheader,nounits

    [pscustomobject]@{
        Image = $Image
        ImageSizeBytes = $imageSize
        Model = $response.model
        Device = $response.device
        ComputeType = $response.compute_type
        AudioDurationSec = $response.duration
        InferenceMs = $response.elapsed_ms
        RequestWallMs = $wallMs
        Language = $response.language
        SegmentCount = @($response.segments).Count
        Text = $response.text
        RuntimeUid = $runtimeUser[0]
        RuntimeGid = $runtimeUser[1]
        ActiveGpuMemoryMiB = ($gpuMemory -join ",")
    } | Format-List

    Write-Host "GPU smoke PASS"
} finally {
    & $docker rm -f $containerName 2>$null | Out-Null
}
