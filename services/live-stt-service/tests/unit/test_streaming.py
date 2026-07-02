"""#128 streaming port tests — GPU-free (no model load)."""

# ruff: noqa: RUF001 - intentional Turkish strings in fixtures.

from __future__ import annotations

from app.api import stream as stream_api
from app.core.config import Settings
from app.services.hallucination import is_hallucination
from app.services.streaming_models import (
    DirectWhisperService,
    get_final_service,
    get_live_service,
)


def test_hallucination_filter_blocks_known_artifacts() -> None:
    assert is_hallucination("") is True
    assert is_hallucination("..") is True
    assert is_hallucination("Altyazı M.K.") is True
    assert is_hallucination("İzlediğiniz için teşekkür ederim.") is True
    assert is_hallucination("Videoyu beğenmeyi unutmayın arkadaşlar") is True
    assert is_hallucination("Thank you for watching") is True


def test_hallucination_filter_passes_real_speech() -> None:
    assert is_hallucination("Toplantı yarın saat onda başlayacak.") is False
    assert is_hallucination("Bütçe raporunu cuma günü teslim edelim.") is False


def test_streaming_defaults_follow_adr_0031() -> None:
    s = Settings()
    assert s.live_model_name == "medium"
    assert s.live_compute_type == "int8"
    assert "large-v3-turbo" in s.final_model_name
    assert s.final_compute_type == "float16"
    assert s.stream_debug is False  # KVKK: verbose debug opt-in only
    assert s.cors_origins == ""  # CORS disabled unless configured
    assert s.live_infer_interval_ms <= 400
    assert s.live_window_sec <= 2.5
    assert s.silence_commit_sec <= 1.0
    assert int(0.5 * stream_api.SAMPLE_RATE) >= int(s.min_infer_sec * stream_api.SAMPLE_RATE)


def test_live_and_final_services_are_distinct_singletons() -> None:
    s = Settings()
    live = get_live_service(s)
    final = get_final_service(s)
    assert isinstance(live, DirectWhisperService)
    assert live is get_live_service(s)  # cached
    assert final is get_final_service(s)
    assert live is not final
    assert live.model_loaded is False  # nothing loaded at construction
    assert final.model_loaded is False


def test_stream_router_importable_without_gpu() -> None:
    from app.api.stream import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert "/ws/stream" in paths
