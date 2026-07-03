"""#128 streaming port tests — GPU-free (no model load)."""

# ruff: noqa: RUF001 - intentional Turkish strings in fixtures.

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from app.api import stream as stream_api
from app.api.stream import (
    _append_recent_final_text,
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
    assert is_hallucination("Neroba") is True


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
    assert is_hallucination("Merhabalar. sesim... gel... Merhabalar sesim geliyor mu?") is True


def test_hallucination_filter_passes_real_speech() -> None:
    assert is_hallucination("Toplantı yarın saat onda başlayacak.") is False
    assert is_hallucination("Bütçe raporunu cuma günü teslim edelim.") is False
    assert (
        is_hallucination("Bugün toplantı kaydında canlı transkript gecikmesini test ediyoruz.")
        is False
    )
    assert is_hallucination("Kelime akışı aktif ve doğruluk oranı gayet iyi.") is False
    assert is_hallucination("Merhaba burada hava çok.") is False
    assert (
        is_hallucination(
            "Enteresan her kelimenin başına merhaba atıyorsun. "
            "Çok değişik şeyler yapabiliyor musun sen de?"
        )
        is False
    )


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


def test_commit_text_uses_two_word_draft_when_final_is_repeated_loop() -> None:
    assert (
        _select_commit_text(
            "Akşama aktif diyorsun Akşam aktif diyorsun ya "
            "Akşama aktif diyorsun yani Akışa aktif diyorsun yani.",
            "devam edelim",
        )
        == "devam edelim"
    )


def test_commit_text_uses_short_draft_when_final_is_single_word_artifact() -> None:
    assert _select_commit_text("Neroba", "Merhaba") == "Merhaba"


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


def test_partial_text_appends_short_no_overlap_continuation_after_stable_draft() -> None:
    assert (
        _merge_rolling_partial(
            "Konuşulanların çok büyük kısmı yazılmıyor",
            "özellikle ara kelimeler düşüyor",
        )
        == "Konuşulanların çok büyük kısmı yazılmıyor özellikle ara kelimeler düşüyor"
    )
    assert (
        _select_partial_text(
            "özellikle ara kelimeler düşüyor",
            "Konuşulanların çok büyük kısmı yazılmıyor",
        )
        == "Konuşulanların çok büyük kısmı yazılmıyor özellikle ara kelimeler düşüyor"
    )


def test_partial_text_appends_one_word_no_overlap_continuation_after_stable_draft() -> None:
    assert (
        _merge_rolling_partial(
            "Konuşulanların çok büyük kısmı yazılmıyor",
            "düşüyor",
        )
        == "Konuşulanların çok büyük kısmı yazılmıyor düşüyor"
    )
    assert (
        _select_partial_text(
            "düşüyor",
            "Konuşulanların çok büyük kısmı yazılmıyor",
        )
        == "Konuşulanların çok büyük kısmı yazılmıyor düşüyor"
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


def test_drop_leading_tail_overlap_can_remove_single_word_carry_over() -> None:
    previous = "Ben sana bir kelime merhaba dedim. Sen uc tane ayri merhaba."
    assert (
        _drop_leading_tail_overlap(
            previous,
            "Merhaba enteresan seyler yapabiliyor musun?",
            allow_single_word=True,
        )
        == "enteresan seyler yapabiliyor musun?"
    )
    assert _drop_leading_tail_overlap(previous, "Merhaba", allow_single_word=True) == "Merhaba"


def test_drop_leading_tail_overlap_handles_turkish_inflected_carry_over() -> None:
    previous = "Beni anlıyor musun? Söylediklerimin yarısı."
    assert (
        _drop_leading_tail_overlap(
            previous,
            "Söylediklerimin yarısını neden yok?",
            allow_single_word=True,
        )
        == "neden yok?"
    )


def test_drop_leading_tail_overlap_does_not_fuzzy_drop_single_word_repeats() -> None:
    assert (
        _drop_leading_tail_overlap(
            "Ben bir kelime merhaba dedim.",
            "Merhabayı başa tekrar yazma.",
            allow_single_word=True,
        )
        == "Merhabayı başa tekrar yazma."
    )


def test_recent_final_tail_catches_cumulative_cross_segment_carry_over() -> None:
    recent = _append_recent_final_text("", "Merhaba.")
    assert recent == "Merhaba."

    second = _drop_leading_tail_overlap(
        recent,
        "Merhaba burada hava cok.",
        allow_single_word=True,
    )
    assert second == "burada hava cok."

    recent = _append_recent_final_text(recent, second)
    assert (
        _drop_leading_tail_overlap(
            recent,
            "Merhaba burada hava cok degisik seyler oluyor.",
            allow_single_word=True,
        )
        == "degisik seyler oluyor."
    )


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
    assert s.live_beam_size == 1
    assert "large-v3-turbo" in s.final_model_name
    assert s.final_compute_type == "float16"
    assert s.final_beam_size == 1
    assert s.stream_debug is False  # KVKK: verbose debug opt-in only
    assert s.stream_final_vad_filter is False
    assert s.cors_origins == ""  # CORS disabled unless configured
    assert s.live_infer_interval_ms <= 700
    assert s.live_window_sec <= 2.5
    assert s.final_window_sec <= 6.0
    assert s.forced_commit_sec <= 5.0
    assert s.silence_commit_sec <= 1.0
    assert s.tail_overlap_sec <= 0.35
    assert s.silence_rms <= 0.001
    assert s.min_speech_rms <= 0.0015
    assert int(0.5 * stream_api.SAMPLE_RATE) >= int(s.min_infer_sec * stream_api.SAMPLE_RATE)


def test_live_and_final_services_are_distinct_singletons() -> None:
    s = Settings()
    live = get_live_service(s)
    final = get_final_service(s)
    assert isinstance(live, DirectWhisperService)
    assert live.beam_size == 1
    assert final.beam_size == 1
    assert live is get_live_service(s)  # cached
    assert final is get_final_service(s)
    assert live is not final
    assert live.model_loaded is False  # nothing loaded at construction
    assert final.model_loaded is False


def test_direct_stream_service_passes_role_specific_beam_size() -> None:
    service = DirectWhisperService(
        model_name="test-model",
        device="cpu",
        compute_type="int8",
        language="tr",
        beam_size=5,
    )

    class FakeModel:
        kwargs: dict[str, object] | None = None

        def transcribe(self, _audio: object, **kwargs: object) -> tuple[list[object], object]:
            self.kwargs = kwargs
            return [SimpleNamespace(text="Merhaba")], object()

    fake_model = FakeModel()
    service._model = fake_model

    result = service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=True)

    assert result == "Merhaba"
    assert fake_model.kwargs is not None
    assert fake_model.kwargs["beam_size"] == 5
    assert fake_model.kwargs["language"] == "tr"
    assert fake_model.kwargs["condition_on_previous_text"] is False


def test_stream_router_importable_without_gpu() -> None:
    from app.api.stream import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert "/ws/stream" in paths
