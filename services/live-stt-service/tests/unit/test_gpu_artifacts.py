from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]


def test_gpu_dockerfile_pins_cuda_cudnn_runtime_and_non_root_user() -> None:
    dockerfile = (SERVICE_ROOT / "Dockerfile.gpu").read_text(encoding="utf-8")

    assert "nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04@sha256:" in dockerfile
    assert "libcublas.so.12" in dockerfile
    assert "libcudnn.so.8" in dockerfile
    assert "USER stt" in dockerfile
    assert "STT_DEVICE=cuda" in dockerfile
    assert "STT_COMPUTE_TYPE=float16" in dockerfile
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility,video" in dockerfile


def test_gpu_smoke_is_fail_fast_and_checks_real_transcription() -> None:
    smoke = (SERVICE_ROOT / "scripts" / "gpu-smoke.ps1").read_text(encoding="utf-8")

    assert '$ErrorActionPreference = "Stop"' in smoke
    assert "--gpus all" in smoke
    assert "get_cuda_device_count()" in smoke
    assert "/transcribe" in smoke
    assert "-hwaccels" in smoke
    assert "cuvid" in smoke
    assert "nvenc" in smoke
    assert "GPU smoke PASS" in smoke
    assert "finally" in smoke


def test_gpu_requirements_pin_ctranslate2() -> None:
    requirements = (SERVICE_ROOT / "requirements-gpu.txt").read_text(encoding="utf-8")

    assert "-r requirements.txt" in requirements
    assert "ctranslate2==4.8.0" in requirements
