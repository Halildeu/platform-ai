"""#128 streaming port tests — GPU-free (no model load)."""

# ruff: noqa: RUF001 - intentional Turkish strings in fixtures.

from __future__ import annotations

from app.api import stream as stream_api
from app.api.stream import (
    _drop_leading_tail_overlap,
    _merge_final_transcript,
    _merge_rolling_partial,
    _select_commit_text,
    _select_partial_text,
)
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


def test_hallucination_filter_blocks_repeated_decode_loops() -> None:
    assert (
        is_hallucination(
            "Akşama aktif diyorsun Akşam aktif diyorsun ya "
            "Akşama aktif diyorsun yani Akışa aktif diyorsun yani."
        )
        is True
    )
    assert (
        is_hallucination(
            "Benim akışa aktiftim. Benim akışa aktif diyorsun. " "Elime akışı aktif diyorsunuz."
        )
        is True
    )
    assert (
        is_hallucination(
            "Kendime Kendimi al. Kendime akışa. Kendime akış al. "
            "Kendime akışa akışa Kendimi akışa aktif. Kelime akışı aktif."
        )
        is True
    )
    assert (
        is_hallucination(
            "Söylediklerimin yarısını ne söylediklerimin yarısını neden "
            "söylediklerimin yarısını neden yok?"
        )
        is True
    )


def test_hallucination_filter_blocks_repeated_alternative_chains() -> None:
    assert (
        is_hallucination(
            "Kelime akışı aktif. Kelime akış aktif diyorsun ya "
            "Kelime akışı aktif diyorsunuz yani kelime akışı aktif."
        )
        is True
    )
    assert (
        is_hallucination(
            "Akşama aktif diyorsun Akşam aktif diyorsun ya "
            "Akşama aktif diyorsun yani Akışa aktif diyorsun yani. "
            "bakışı aktif diyorsun yani."
        )
        is True
    )


def test_hallucination_filter_passes_real_speech() -> None:
    assert is_hallucination("Toplantı yarın saat onda başlayacak.") is False
    assert is_hallucination("Bütçe raporunu cuma günü teslim edelim.") is False
    assert (
        is_hallucination("Bugün toplantı kaydında canlı transkript gecikmesini test ediyoruz.")
        is False
    )
    assert is_hallucination("Kelime akışı aktif ve doğruluk oranı gayet iyi.") is False


def test_commit_text_falls_back_to_clean_draft_when_final_is_repeated_loop() -> None:
    assert (
        _select_commit_text(
            "Benim akışa aktiftim. Benim akışa aktif diyorsun. " "Elime akışı aktif diyorsunuz.",
            "Kelime akışı aktif ve doğruluk oranı gayet iyi.",
        )
        == "Kelime akışı aktif ve doğruluk oranı gayet iyi."
    )


def test_commit_text_drops_segment_when_final_and_draft_are_unusable() -> None:
    assert (
        _select_commit_text(
            "Benim akışa aktiftim. Benim akışa aktif diyorsun. " "Elime akışı aktif diyorsunuz.",
            "Altyazı M.K.",
        )
        is None
    )


def test_commit_text_does_not_finalize_short_draft_when_final_is_repeated_loop() -> None:
    assert (
        _select_commit_text(
            "Akşama aktif diyorsun Akşam aktif diyorsun ya "
            "Akşama aktif diyorsun yani Akışa aktif diyorsun yani.",
            "Böyle...",
        )
        is None
    )


def test_partial_text_keeps_live_draft_word_progressive() -> None:
    assert _select_partial_text("Merhaba", "") == "Merhaba"
    assert _select_partial_text("Merhaba nasılsın", "Merhaba") == "Merhaba nasılsın"
    assert _select_partial_text("Merhaba", "Merhaba nasılsın") is None
    assert _select_partial_text("Merhaba iyi misin", "Merhaba nasılsın") == "Merhaba iyi misin"


def test_partial_text_merges_rolling_window_overlap_without_dropping_prefix() -> None:
    assert (
        _merge_rolling_partial(
            "Bugün toplantıda hızlı şekilde",
            "hızlı şekilde yazıya dönüşüyor",
        )
        == "Bugün toplantıda hızlı şekilde yazıya dönüşüyor"
    )
    assert (
        _select_partial_text(
            "hızlı şekilde yazıya dönüşüyor",
            "Bugün toplantıda hızlı şekilde",
        )
        == "Bugün toplantıda hızlı şekilde yazıya dönüşüyor"
    )


def test_commit_text_applies_final_suffix_without_dropping_live_prefix() -> None:
    assert (
        _merge_final_transcript(
            "Bu cümle doğru şekilde yazılıyor",
            "doğru şekilde yazılıyor.",
        )
        == "Bu cümle doğru şekilde yazılıyor."
    )
    assert (
        _select_commit_text(
            "doğru şekilde yazılıyor.",
            "Bu cümle doğru şekilde yazılıyor",
        )
        == "Bu cümle doğru şekilde yazılıyor."
    )


def test_drop_leading_tail_overlap_removes_cross_segment_repeated_word() -> None:
    assert _drop_leading_tail_overlap("Merhaba.", "Merhaba burada hava çok") == "burada hava çok"
    assert (
        _drop_leading_tail_overlap(
            "Bugün canlı transkript gecikmesini test ediyoruz.",
            "test ediyoruz ve doğruluk daha iyi görünüyor.",
        )
        == "ve doğruluk daha iyi görünüyor."
    )
    assert _drop_leading_tail_overlap("İlk konu tamam.", "İkinci konu başladı.") == (
        "İkinci konu başladı."
    )
    assert _drop_leading_tail_overlap("Final 1.", "Final 2.") == "Final 2."


def test_commit_text_blocks_repeated_final_and_bad_rolling_draft() -> None:
    repeated = (
        "Akşama aktif diyorsun Akşam aktif diyorsun ya "
        "Akşama aktif diyorsun yani Akışa aktif diyorsun yani. "
        "bakışı aktif diyorsun yani."
    )
    assert _select_commit_text(repeated, repeated) is None


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
