"""#128 streaming port tests — GPU-free (no model load)."""

# ruff: noqa: RUF001 - intentional Turkish strings in fixtures.

from __future__ import annotations

import asyncio
import hashlib
import queue
import stat
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.api import stream as stream_api
from app.api.stream import (
    _append_recent_final_text,
    _decode_client_control,
    _drop_leading_tail_overlap,
    _merge_final_transcript,
    _merge_rolling_partial,
    _select_commit_text,
    _select_partial_parts,
    _select_partial_text,
    _stabilize_rolling_partial,
    _transcribe_with_stream_generation,
)
from app.core import config as config_module
from app.core.config import Settings
from app.services import streaming_models as streaming_models_module
from app.services.hallucination import (
    is_contextual_silence_hallucination,
    is_hallucination,
)
from app.services.model_preload import StreamingPreloadState
from app.services.streaming_models import (
    DirectWhisperService,
    SupervisedFinalWhisperService,
    SupervisedLiveWhisperService,
    get_final_service,
    get_live_service,
    shutdown_streaming_services,
    streaming_services_healthy,
)
from app.services.worker import WorkerCrashedError, WorkerTimeoutError


@pytest.fixture(autouse=True)
def use_inline_final_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_STREAM_LIVE_WORKER_BACKEND", "inline")
    monkeypatch.setenv("STT_STREAM_FINAL_WORKER_BACKEND", "inline")
    config_module._settings = None


def test_hallucination_filter_blocks_known_artifacts() -> None:
    assert is_hallucination("") is True
    assert is_hallucination("..") is True
    assert is_hallucination("Altyazı M.K.") is True
    assert is_hallucination("Videoyu beğenmeyi unutmayın arkadaşlar") is True
    assert is_hallucination("Neroba") is True


def test_context_control_is_bounded_and_normalized() -> None:
    control_type, hotwords = _decode_client_control(
        '{"type":"context","terms":["  Çağrı Öztürk ","Proje-24"]}'
    )

    assert control_type == "context"
    assert hotwords == "Çağrı Öztürk, Proje-24"


def test_direct_stream_passes_context_to_faster_whisper() -> None:
    service = DirectWhisperService("tiny", "cpu", "int8", "tr", 1)

    service.transcribe_array(np.ones(160, dtype=np.float32), False, "Çağrı Öztürk")

    assert service._model is not None
    assert service._model.__class__.last_kwargs["hotwords"] == "Çağrı Öztürk"


def test_preload_failure_rejects_websocket_without_lazy_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production admission must not bypass the exhausted startup budget."""

    class _WebSocket:
        def __init__(self) -> None:
            state = StreamingPreloadState(enabled=True)
            state.mark_failed("live")
            self.app = SimpleNamespace(state=SimpleNamespace(streaming_preload=state))
            self.query_params = {"protocol": stream_api.STREAM_PROTOCOL}
            self.events: list[dict[str, object]] = []
            self.close_code: int | None = None

        async def accept(self) -> None:
            return None

        async def send_json(self, event: dict[str, object]) -> None:
            self.events.append(event)

        async def close(self, *, code: int = 1000) -> None:
            self.close_code = code

    def unexpected_model_access(_settings: Settings) -> object:
        raise AssertionError("failed preload must not invoke a lazy model factory")

    monkeypatch.setattr(stream_api, "get_live_service", unexpected_model_access)
    monkeypatch.setattr(stream_api, "get_final_service", unexpected_model_access)
    websocket = _WebSocket()

    asyncio.run(
        stream_api.stream_endpoint(
            websocket,  # type: ignore[arg-type]
            Settings(stream_preload_models=True),
        )
    )

    assert websocket.events == [{"type": "error", "msg": "service_not_ready"}]
    assert websocket.close_code == 1013


def test_hallucination_filter_keeps_valid_short_turkish_utterances() -> None:
    # #238: short acknowledgements / clarification responses are real speech.
    for utterance in ("Ne?", "Ha?", "He.", "Yok.", "Evet", "Tamam", "Peki", "hı"):
        assert is_hallucination(utterance) is False, utterance


def test_hallucination_filter_still_blocks_short_artifacts() -> None:
    # #238 regression: the allowlist must not re-admit the short artifact
    # finals the length/pattern guards exist for.
    for artifact in ("", ".", "..", "!?", "cis", "ces", "neroba"):
        assert is_hallucination(artifact) is True, artifact


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
    assert is_hallucination("İzlediğiniz için teşekkür ederim.") is False
    assert is_hallucination("İstediğiniz için teşekkür ederim.") is False
    assert is_hallucination("Teşekkür ederim.") is False
    assert is_hallucination("Görüşürüz.") is False
    assert is_hallucination("İyi günler.") is False
    assert is_hallucination("Thank you for the detailed review.") is False
    assert (
        is_hallucination(
            "Enteresan her kelimenin başına merhaba atıyorsun. "
            "Çok değişik şeyler yapabiliyor musun sen de?"
        )
        is False
    )


@pytest.mark.parametrize(
    ("text", "audio_rms", "no_speech_prob", "expected"),
    [
        ("İzlediğiniz için teşekkür ederim.", 0.0114, 0.65, True),
        (
            "İzlediğiniz için teşekkür ederim. İzlediğiniz için teşekkür ederim.",
            0.0114,
            0.65,
            True,
        ),
        ("İzlediğiniz için teşekkür ederim.", 0.1063, 0.65, False),
        ("İzlediğiniz için teşekkür ederim.", 0.0114, 0.59, False),
        ("İstediğiniz için teşekkür ederim.", 0.0114, 0.65, False),
    ],
)
def test_contextual_silence_filter_requires_all_three_signals(
    text: str,
    audio_rms: float,
    no_speech_prob: float,
    expected: bool,
) -> None:
    assert (
        is_contextual_silence_hallucination(
            text,
            audio_rms=audio_rms,
            no_speech_prob=no_speech_prob,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("audio_rms", "expected"),
    [
        (0.0114, ""),
        (0.1063, "İzlediğiniz için teşekkür ederim."),
    ],
)
def test_streaming_contextual_filter_uses_input_rms(
    audio_rms: float,
    expected: str,
) -> None:
    service = DirectWhisperService(
        model_name="test-model",
        device="cpu",
        compute_type="int8",
        language="tr",
        beam_size=1,
    )

    class FakeModel:
        def transcribe(self, _audio: object, **_kwargs: object) -> tuple[list[object], object]:
            return [
                SimpleNamespace(
                    text="İzlediğiniz için teşekkür ederim.",
                    no_speech_prob=0.65,
                    avg_logprob=-0.2,
                )
            ], object()

    service._model = FakeModel()
    audio = np.full(1600, audio_rms, dtype=np.float32)

    assert service.transcribe_array(audio, vad=False) == expected


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


def test_partial_stabilizer_promotes_overlap_and_keeps_new_tail_tentative() -> None:
    assert _stabilize_rolling_partial(
        "",
        "Bugün toplantıda hızlı şekilde",
        "hızlı şekilde yazıya dönüşüyor",
    ) == (
        "Bugün toplantıda hızlı şekilde",
        "yazıya dönüşüyor",
    )


def test_partial_stabilizer_replaces_competing_same_opener_tail() -> None:
    assert _stabilize_rolling_partial(
        "",
        "Merhaba burada hava çok",
        "Merhaba atıyorsun çok değişik şeyler",
    ) == (
        "Merhaba",
        "atıyorsun çok değişik şeyler",
    )


def test_partial_stabilizer_strips_confirmed_context_from_full_window() -> None:
    assert _stabilize_rolling_partial(
        "Merhaba",
        "nasılsın",
        "Merhaba nasılsın bugün",
    ) == (
        "Merhaba nasılsın",
        "bugün",
    )


def test_partial_stabilizer_recovers_words_around_confirmed_middle_context() -> None:
    assert _stabilize_rolling_partial(
        "hızlı şekilde",
        "",
        "bugün hızlı şekilde yazıya dönüşüyor",
    ) == (
        "bugün hızlı şekilde",
        "yazıya dönüşüyor",
    )


def test_partial_stabilizer_replaces_overlapping_alternative_without_appending() -> None:
    assert _stabilize_rolling_partial(
        "",
        "bütçe raporunu cuma teslim edelim",
        "raporu cuma günü teslim edelim",
    ) == (
        "",
        "raporu cuma günü teslim edelim",
    )


def test_partial_selection_does_not_drop_legitimate_common_meeting_phrase() -> None:
    assert _select_partial_parts("Teşekkür ederim.", "", "") == ("", "Teşekkür ederim.")


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


def test_commit_text_keeps_medium_draft_over_short_unrelated_final() -> None:
    draft = "Konuşulanların çok büyük kısmı yazılmıyor ara kelimeler düşüyor"
    final = "Görüşmek üzere canı çıkmak için"

    assert _merge_final_transcript(draft, final) == draft
    assert _select_commit_text(final, draft) == draft


def test_commit_text_still_allows_short_final_with_shared_context() -> None:
    draft = "Merhaba sesim iyi geliyor mu yanlış"
    final = "Merhaba sesim geliyor mu?"

    assert _merge_final_transcript(draft, final) == final
    assert _select_commit_text(final, draft) == final


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
    s = Settings(
        stream_live_worker_backend="process",
        stream_final_worker_backend="process",
    )
    assert s.live_model_name == "medium"
    assert s.live_compute_type == "int8"
    assert s.live_beam_size == 1
    assert "large-v3-turbo" in s.final_model_name
    assert s.final_compute_type == "float16"
    assert s.final_beam_size == 1
    assert s.stream_debug is False  # KVKK: verbose debug opt-in only
    assert s.stream_live_vad_filter is False
    assert s.stream_final_vad_filter is False
    assert s.stream_vad_min_silence_duration_ms < int(s.live_window_sec * 1000)
    assert s.stream_final_worker_backend == "process"
    assert s.stream_live_worker_backend == "process"
    assert s.stream_live_timeout_sec == 5.0
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


def test_supervised_final_worker_timeout_terminates_and_respawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedFinalWhisperService)
    service._timeout_sec = 0.01
    service._load_timeout_sec = 0.01
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._model_loaded = True
    service._generation = 1
    old_process = service._process
    restarts: list[FakeProcess] = []

    def fake_start() -> None:
        process = FakeProcess()
        restarts.append(process)
        service._process = process
        service._result_queue = queue.Queue(maxsize=1)

        class ResponsiveTaskQueue(queue.Queue[dict[str, object]]):
            def put(  # type: ignore[override]
                self,
                item: dict[str, object],
                block: bool = True,
                timeout: float | None = None,
            ) -> None:
                del block, timeout
                service._result_queue.put(
                    {
                        "job_id": item["job_id"],
                        "ok": True,
                        "text": "Yeniden başlayan worker yanıtı",
                    }
                )

        service._task_queue = ResponsiveTaskQueue(maxsize=1)
        service._model_loaded = False

    monkeypatch.setattr(service, "_start", fake_start)

    with pytest.raises(WorkerTimeoutError, match="exceeded timeout"):
        service.transcribe_array(np.ones(1600, dtype=np.float32), vad=False)

    assert old_process.terminated is True
    assert old_process.killed is True
    assert len(restarts) == 1
    assert service._process is restarts[0]
    assert service.model_loaded is False

    recovered = service.transcribe_array(np.ones(1600, dtype=np.float32), vad=False)

    assert recovered == "Yeniden başlayan worker yanıtı"
    assert service.model_loaded is True


def test_supervised_final_worker_does_not_respawn_when_kill_cannot_stop_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnkillableProcess:
        terminated = False
        killed = False

        def is_alive(self) -> bool:
            return True

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedFinalWhisperService)
    service._timeout_sec = 0.01
    service._load_timeout_sec = 0.01
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._process = UnkillableProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._model_loaded = True
    service._generation = 1
    service._restart_blocked = False
    starts: list[None] = []
    monkeypatch.setattr(service, "_start", lambda: starts.append(None))

    with pytest.raises(WorkerCrashedError, match="could not be stopped safely"):
        service.transcribe_array(np.ones(1600, dtype=np.float32), vad=False)

    assert service._process.terminated is True
    assert service._process.killed is True
    assert service._restart_blocked is True
    assert service.model_loaded is False
    assert starts == []

    with pytest.raises(WorkerCrashedError, match="restart is blocked"):
        service.transcribe_array(np.ones(1600, dtype=np.float32), vad=False)
    assert starts == []


def test_supervised_live_worker_timeout_kills_native_child_without_spawning_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        alive = True
        terminated = False
        killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._timeout_sec = 0.01
    service._load_timeout_sec = 0.01
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._model_loaded = True
    service._restart_blocked = False
    service._generation = 1
    old_process = service._process
    starts: list[None] = []
    monkeypatch.setattr(service, "_start", lambda: starts.append(None))

    with pytest.raises(WorkerTimeoutError, match="streaming live worker exceeded timeout"):
        service.transcribe_array(np.ones(1600, dtype=np.float32), vad=False)

    assert old_process.terminated is True
    assert old_process.killed is True
    assert starts == [None]


def test_waiting_live_call_reloads_recycled_worker_before_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def is_alive(self) -> bool:
            return True

    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._timeout_sec = 0.5
    service._load_timeout_sec = 0.5
    service._call_lock = threading.Lock()
    service._process = FakeProcess()
    service._model_loaded = True
    service._restart_blocked = False
    service._generation = 1
    service._closing = threading.Event()
    first_entered = threading.Event()
    release_first = threading.Event()
    operations: list[str] = []
    results: list[BaseException | str] = []
    transcribe_calls = 0

    def fake_invoke_locked(operation: str, **_kwargs: object) -> str:
        nonlocal transcribe_calls
        operations.append(operation)
        if operation == "load":
            service._model_loaded = True
            return ""
        transcribe_calls += 1
        if transcribe_calls == 1:
            first_entered.set()
            assert release_first.wait(timeout=1.0)
            service._model_loaded = False
            service._generation += 1
            raise WorkerTimeoutError("first inference recycled worker")
        assert service._model_loaded is True
        return "recovered"

    monkeypatch.setattr(service, "_invoke_locked", fake_invoke_locked)

    def transcribe() -> None:
        try:
            results.append(service.transcribe_array(np.ones(1600, dtype=np.float32), vad=False))
        except BaseException as exc:  # noqa: BLE001 - assertion captures thread result
            results.append(exc)

    first = threading.Thread(target=transcribe)
    second = threading.Thread(target=transcribe)
    first.start()
    assert first_entered.wait(timeout=1.0)
    second.start()
    release_first.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert any(isinstance(result, WorkerTimeoutError) for result in results)
    assert "recovered" in results
    assert operations == ["transcribe", "load", "transcribe"]


def test_load_failure_recycles_worker_before_preload_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        alive = True
        terminated = False
        killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._load_timeout_sec = 1.0
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._closing = threading.Event()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._result_queue.put({"job_id": "load-job", "ok": False, "error_class": "RuntimeError"})
    service._model_loaded = False
    service._restart_blocked = False
    service._generation = 1
    old_process = service._process
    starts: list[None] = []
    monkeypatch.setattr(streaming_models_module.uuid, "uuid4", lambda: "load-job")
    monkeypatch.setattr(service, "_start", lambda: starts.append(None))

    with pytest.raises(RuntimeError, match="RuntimeError"):
        service.ensure_model()

    assert old_process.terminated is True
    assert old_process.killed is True
    assert starts == [None]


def test_transcribe_failure_invalidates_generation_until_explicit_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._timeout_sec = 0.5
    service._load_timeout_sec = 0.5
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._closing = threading.Event()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._result_queue.put({"job_id": "transcribe-job", "ok": False, "error_class": "CudaError"})
    service._model_loaded = True
    service._restart_blocked = False
    service._generation = 11
    old_process = service._process
    starts: list[None] = []
    operations: list[str] = []

    class ResponsiveTaskQueue(queue.Queue[dict[str, object]]):
        def put(  # type: ignore[override]
            self,
            item: dict[str, object],
            block: bool = True,
            timeout: float | None = None,
        ) -> None:
            del block, timeout
            operation = str(item["type"])
            operations.append(operation)
            service._result_queue.put(
                {
                    "job_id": item["job_id"],
                    "ok": True,
                    "text": "recovered" if operation == "transcribe" else "",
                }
            )

    def fake_start() -> None:
        starts.append(None)
        service._generation += 1
        service._process = FakeProcess()
        service._result_queue = queue.Queue(maxsize=1)
        service._task_queue = ResponsiveTaskQueue(maxsize=1)
        service._model_loaded = False
        service._restart_blocked = False

    monkeypatch.setattr(streaming_models_module.uuid, "uuid4", lambda: "transcribe-job")
    monkeypatch.setattr(service, "_start", fake_start)

    saved_live = dict(streaming_models_module._supervised_live_services)
    saved_final = dict(streaming_models_module._supervised_final_services)
    streaming_models_module._supervised_live_services.clear()
    streaming_models_module._supervised_final_services.clear()
    streaming_models_module._supervised_live_services["failed-generation"] = service
    try:
        with pytest.raises(RuntimeError, match="CudaError"):
            service.transcribe_loaded_array(
                np.ones(1600, dtype=np.float32),
                vad=False,
                expected_generation=11,
            )

        assert old_process.terminated is True
        assert old_process.killed is True
        assert service._generation != 11
        assert service.model_loaded is False
        assert streaming_services_healthy() is False
        assert starts == []

        # The failed active connection cannot retry or lazy-load against its
        # captured generation. Recovery belongs to the lifecycle owner.
        with pytest.raises(WorkerCrashedError, match="readiness changed"):
            service.transcribe_loaded_array(
                np.ones(1600, dtype=np.float32),
                vad=False,
                expected_generation=11,
            )
        assert starts == []
        assert operations == []

        service.ensure_model()
        recovered_generation = service.ready_generation
        assert starts == [None]
        assert recovered_generation != 11
        assert service.model_loaded is True
        assert streaming_services_healthy() is True
        assert (
            service.transcribe_loaded_array(
                np.ones(1600, dtype=np.float32),
                vad=False,
                expected_generation=recovered_generation,
            )
            == "recovered"
        )
        assert operations == ["load", "transcribe"]
    finally:
        streaming_models_module._supervised_live_services.clear()
        streaming_models_module._supervised_live_services.update(saved_live)
        streaming_models_module._supervised_final_services.clear()
        streaming_models_module._supervised_final_services.update(saved_final)


def test_loaded_transcribe_failure_invalidates_generation_without_stream_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        alive = True
        terminated = False
        killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._timeout_sec = 1.0
    service._load_timeout_sec = 1.0
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._closing = threading.Event()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._result_queue.put({"job_id": "transcribe-job", "ok": False, "error_class": "CudaError"})
    service._model_loaded = True
    service._restart_blocked = False
    service._generation = 7
    starts: list[None] = []
    monkeypatch.setattr(streaming_models_module.uuid, "uuid4", lambda: "transcribe-job")
    monkeypatch.setattr(service, "_start", lambda: starts.append(None))

    with pytest.raises(RuntimeError, match="CudaError"):
        service.transcribe_loaded_array(
            np.ones(1600, dtype=np.float32), vad=False, expected_generation=7
        )

    assert service._process is None
    assert service._model_loaded is False
    assert starts == []


def test_preload_lock_and_load_share_the_callers_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._load_timeout_sec = 180.0
    service._kill_grace_sec = 2.0
    service._call_lock = threading.Lock()
    observed: list[tuple[str, float]] = []

    def acquire(deadline: float) -> None:
        observed.append(("lock", deadline))
        assert service._call_lock.acquire(timeout=0)

    def load(
        deadline: float | None = None,
        cleanup_deadline: float | None = None,
    ) -> None:
        assert deadline is not None
        assert cleanup_deadline is not None
        observed.append(("load", deadline))
        observed.append(("cleanup", cleanup_deadline))

    monkeypatch.setattr(service, "_acquire_call_lock", acquire)
    monkeypatch.setattr(service, "_ensure_model_locked", load)
    service.ensure_model(deadline=12345.0)

    assert observed == [
        ("lock", 12345.0),
        ("load", 12345.0),
        ("cleanup", 12349.0),
    ]


def test_streaming_readiness_requires_loaded_current_workers() -> None:
    saved_live = dict(streaming_models_module._supervised_live_services)
    saved_final = dict(streaming_models_module._supervised_final_services)
    streaming_models_module._supervised_live_services.clear()
    streaming_models_module._supervised_final_services.clear()
    try:
        streaming_models_module._supervised_live_services["cold"] = SimpleNamespace(
            healthy=True,
            model_loaded=False,
        )
        assert streaming_services_healthy() is False
        streaming_models_module._supervised_live_services["cold"].model_loaded = True
        assert streaming_services_healthy() is True
    finally:
        streaming_models_module._supervised_live_services.clear()
        streaming_models_module._supervised_live_services.update(saved_live)
        streaming_models_module._supervised_final_services.clear()
        streaming_models_module._supervised_final_services.update(saved_final)


def test_streaming_model_pin_hashes_exact_model_bytes(tmp_path) -> None:
    model_bin = tmp_path / "model.bin"
    model_bin.write_bytes(b"approved-stream-model")
    config = tmp_path / "config.json"
    config.write_text('{"approved":true}', encoding="utf-8")
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text('{"version":"approved"}', encoding="utf-8")
    digest = hashlib.sha256(model_bin.read_bytes()).hexdigest()
    tree_digest = streaming_models_module._model_tree_sha256(tmp_path)

    assert streaming_models_module._resolve_stream_model_source(
        "floating-name", str(tmp_path), f"sha256:{digest}", f"sha256:{tree_digest}"
    ) == str(tmp_path.resolve())
    config.write_text('{"approved":false}', encoding="utf-8")
    with pytest.raises(ValueError, match="tree SHA-256 mismatch"):
        streaming_models_module._resolve_stream_model_source(
            "floating-name", str(tmp_path), digest, tree_digest
        )
    config.write_text('{"approved":true}', encoding="utf-8")
    tokenizer.write_text('{"version":"mutated"}', encoding="utf-8")
    with pytest.raises(ValueError, match="tree SHA-256 mismatch"):
        streaming_models_module._resolve_stream_model_source(
            "floating-name", str(tmp_path), digest, tree_digest
        )
    tokenizer.write_text('{"version":"approved"}', encoding="utf-8")
    model_bin.write_bytes(b"mutated-stream-model")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        streaming_models_module._resolve_stream_model_source(
            "floating-name", str(tmp_path), digest, tree_digest
        )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        streaming_models_module._resolve_stream_model_source(
            "floating-name", str(tmp_path), "0" * 64
        )


def test_streaming_model_pin_rejects_linked_artifact(tmp_path) -> None:
    model_bin = tmp_path / "model.bin"
    model_bin.write_bytes(b"approved-stream-model")
    target = tmp_path / "tokenizer-target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "tokenizer.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="contains a link"):
        streaming_models_module._model_tree_sha256(tmp_path)


def test_streaming_model_pin_rejects_linked_root(tmp_path) -> None:
    model_root = tmp_path / "model-root"
    model_root.mkdir()
    (model_root / "model.bin").write_bytes(b"approved-stream-model")
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(model_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="link or reparse point"):
        streaming_models_module._resolve_stream_model_source("floating-name", str(linked_root), "")


def test_streaming_model_pin_recognizes_windows_reparse_attribute() -> None:
    reparse_stat = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=streaming_models_module._FILE_ATTRIBUTE_REPARSE_POINT,
    )

    assert streaming_models_module._is_link_or_reparse(reparse_stat) is True


def test_streaming_model_pin_rechecks_tree_after_loader_consumes_files(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_bin = tmp_path / "model.bin"
    model_bin.write_bytes(b"approved-stream-model")
    config = tmp_path / "config.json"
    config.write_text('{"approved":true}', encoding="utf-8")
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text('{"version":"approved"}', encoding="utf-8")
    model_digest = hashlib.sha256(model_bin.read_bytes()).hexdigest()
    tree_digest = streaming_models_module._model_tree_sha256(tmp_path)

    class MutatingWhisperModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            tokenizer.write_text('{"version":"swapped-during-load"}', encoding="utf-8")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=MutatingWhisperModel),
    )
    service = DirectWhisperService(
        "floating-name",
        "cpu",
        "int8",
        "tr",
        1,
        model_path=str(tmp_path),
        model_sha256=model_digest,
        model_tree_sha256=tree_digest,
    )

    with pytest.raises(ValueError, match="tree SHA-256 mismatch"):
        service.ensure_model()
    assert service.model_loaded is False


def test_supervised_worker_close_is_bounded_while_call_lock_is_held() -> None:
    service = object.__new__(SupervisedLiveWhisperService)
    service.role = "live"
    service._call_lock = threading.Lock()
    service._closing = threading.Event()
    service._call_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(WorkerTimeoutError, match="queue exceeded timeout"):
            service.close(deadline=time.monotonic() + 0.02)
    finally:
        service._call_lock.release()

    assert time.monotonic() - started < 0.2
    assert service._closing.is_set()


def test_shutdown_attempts_every_worker_and_keeps_failed_worker_cached() -> None:
    calls: list[str] = []

    class FakeService:
        def __init__(self, role: str, *, fails: bool) -> None:
            self.role = role
            self.fails = fails

        def close(self, *, deadline: float) -> None:
            assert deadline >= time.monotonic()
            calls.append(self.role)
            if self.fails:
                raise WorkerCrashedError("fixture worker remained alive")

    failed = FakeService("live", fails=True)
    closed = FakeService("final", fails=False)
    saved_live = dict(streaming_models_module._supervised_live_services)
    saved_final = dict(streaming_models_module._supervised_final_services)
    streaming_models_module._supervised_live_services.clear()
    streaming_models_module._supervised_final_services.clear()
    streaming_models_module._supervised_live_services["failed"] = failed  # type: ignore[assignment]
    streaming_models_module._supervised_final_services["closed"] = closed  # type: ignore[assignment]
    try:
        with pytest.raises(WorkerCrashedError, match="1 streaming worker"):
            shutdown_streaming_services(timeout_sec=0.2)
        assert calls == ["live", "final"]
        assert streaming_models_module._supervised_live_services == {"failed": failed}
        assert streaming_models_module._supervised_final_services == {}
    finally:
        streaming_models_module._supervised_live_services.clear()
        streaming_models_module._supervised_live_services.update(saved_live)
        streaming_models_module._supervised_final_services.clear()
        streaming_models_module._supervised_final_services.update(saved_final)


def test_terminal_decode_timeout_never_reloads_model_inside_declared_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        alive = True
        terminated = False
        killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.alive = False

        def join(self, *, timeout: float) -> None:
            del timeout

    service = object.__new__(SupervisedFinalWhisperService)
    service._timeout_sec = 0.01
    service._load_timeout_sec = 10.0
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._model_loaded = True
    service._restart_blocked = False
    service._generation = 1
    old_process = service._process
    starts: list[None] = []
    monkeypatch.setattr(service, "_start", lambda: starts.append(None))

    with pytest.raises(WorkerTimeoutError, match="exceeded timeout"):
        service.transcribe_loaded_array(
            np.ones(1600, dtype=np.float32),
            vad=False,
            expected_generation=1,
        )

    assert old_process.terminated is True
    assert old_process.killed is True
    assert starts == []
    assert service.model_loaded is False

    with pytest.raises(WorkerCrashedError, match="readiness changed"):
        service.transcribe_loaded_array(
            np.ones(1600, dtype=np.float32),
            vad=False,
            expected_generation=1,
        )
    assert starts == []


def test_terminal_decode_rechecks_ready_generation_after_waiting_for_lock() -> None:
    class FakeProcess:
        def is_alive(self) -> bool:
            return True

    service = object.__new__(SupervisedFinalWhisperService)
    service._timeout_sec = 0.5
    service._load_timeout_sec = 0.5
    service._kill_grace_sec = 0.0
    service._call_lock = threading.Lock()
    service._process = FakeProcess()
    service._task_queue = queue.Queue(maxsize=1)
    service._result_queue = queue.Queue(maxsize=1)
    service._model_loaded = True
    service._restart_blocked = False
    service._generation = 1
    result: list[BaseException | str] = []

    def terminal_decode() -> None:
        try:
            result.append(
                service.transcribe_loaded_array(
                    np.ones(1600, dtype=np.float32),
                    vad=False,
                    expected_generation=1,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assertion captures thread result
            result.append(exc)

    service._call_lock.acquire()
    thread = threading.Thread(target=terminal_decode)
    thread.start()
    service._generation = 2
    service._model_loaded = False
    service._call_lock.release()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], WorkerCrashedError)
    assert "readiness changed" in str(result[0])
    assert service._task_queue.empty()


@pytest.mark.parametrize(
    "service_type",
    (SupervisedLiveWhisperService, SupervisedFinalWhisperService),
)
def test_stream_decode_uses_pinned_worker_generation_without_lazy_reload(
    service_type: type[SupervisedLiveWhisperService] | type[SupervisedFinalWhisperService],
) -> None:
    class PinnedService(service_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            self.loaded_calls: list[int] = []

        def transcribe_array(
            self,
            audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
            vad: bool,
        ) -> str:
            del audio, vad
            raise AssertionError("accepted streams must not call the lazy reload path")

        def transcribe_loaded_array(
            self,
            audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
            vad: bool,
            expected_generation: int,
        ) -> str:
            del audio, vad
            self.loaded_calls.append(expected_generation)
            return "generation-pinned"

    service = PinnedService()
    result = _transcribe_with_stream_generation(
        service,
        np.ones(1600, dtype=np.float32),
        False,
        17,
    )

    assert result == "generation-pinned"
    assert service.loaded_calls == [17]


def test_stream_decode_fails_when_pinned_generation_is_missing() -> None:
    service = object.__new__(SupervisedLiveWhisperService)

    with pytest.raises(WorkerCrashedError, match="readiness is unavailable"):
        _transcribe_with_stream_generation(
            service,
            np.ones(1600, dtype=np.float32),
            False,
            None,
        )


def test_direct_stream_service_passes_role_specific_beam_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DirectWhisperService(
        model_name="test-model",
        device="cpu",
        compute_type="int8",
        language="tr",
        beam_size=5,
        vad_parameters={
            "threshold": 0.35,
            "min_speech_duration_ms": 100,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 100,
        },
    )

    class FakeModel:
        kwargs: dict[str, object] | None = None

        def transcribe(self, _audio: object, **kwargs: object) -> tuple[list[object], object]:
            self.kwargs = kwargs
            return [SimpleNamespace(text="Merhaba")], object()

    fake_model = FakeModel()
    service._model = fake_model
    observed_vad_parameters: list[dict[str, float | int]] = []

    def pass_vad_audio(
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        parameters: dict[str, float | int],
    ) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
        observed_vad_parameters.append(parameters)
        return audio

    monkeypatch.setattr(streaming_models_module, "_prepare_vad_audio", pass_vad_audio)

    result = service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=True)

    assert result == "Merhaba"
    assert fake_model.kwargs is not None
    assert fake_model.kwargs["beam_size"] == 5
    assert fake_model.kwargs["language"] == "tr"
    assert fake_model.kwargs["condition_on_previous_text"] is False
    assert observed_vad_parameters == [
        {
            "threshold": 0.35,
            "min_speech_duration_ms": 100,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 100,
        }
    ]
    assert fake_model.kwargs["vad_filter"] is False
    assert fake_model.kwargs["vad_parameters"] is None

    service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=False)
    assert fake_model.kwargs["vad_filter"] is False
    assert fake_model.kwargs["vad_parameters"] is None
    assert observed_vad_parameters == [
        {
            "threshold": 0.35,
            "min_speech_duration_ms": 100,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 100,
        }
    ]


@pytest.mark.parametrize(
    "service_type",
    (SupervisedLiveWhisperService, SupervisedFinalWhisperService),
)
def test_supervised_worker_receives_pinned_vad_parameters(
    monkeypatch: pytest.MonkeyPatch,
    service_type: type[SupervisedLiveWhisperService] | type[SupervisedFinalWhisperService],
) -> None:
    monkeypatch.setattr(
        streaming_models_module._SupervisedWhisperService,
        "_start",
        lambda self: None,
    )
    service = service_type(Settings())

    assert service._config["vad_parameters"] == {
        "threshold": 0.35,
        "min_speech_duration_ms": 100,
        "min_silence_duration_ms": 300,
        "speech_pad_ms": 100,
    }


def test_vad_parameters_are_part_of_direct_service_cache_identity() -> None:
    first = get_live_service(Settings(stream_live_worker_backend="inline"))
    same = get_live_service(Settings(stream_live_worker_backend="inline"))
    changed = get_live_service(
        Settings(stream_live_worker_backend="inline", stream_vad_threshold=0.4)
    )

    assert same is first
    assert changed is not first


def test_direct_stream_service_filters_low_confidence_silence_decode() -> None:
    service = DirectWhisperService(
        model_name="test-model",
        device="cpu",
        compute_type="int8",
        language="tr",
        beam_size=1,
    )

    class FakeModel:
        def transcribe(self, _audio: object, **_kwargs: object) -> tuple[list[object], object]:
            return [
                SimpleNamespace(
                    text="Teşekkür ederim.",
                    no_speech_prob=0.9,
                    avg_logprob=-0.2,
                ),
                SimpleNamespace(
                    text="Toplantıya devam edelim.",
                    no_speech_prob=0.05,
                    avg_logprob=-0.2,
                ),
            ], object()

    service._model = FakeModel()

    assert (
        service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=False)
        == "Toplantıya devam edelim."
    )


def test_get_live_and_final_services_thread_decode_thresholds_from_settings() -> None:
    # #237: the live path must read the same operator-tunable decode thresholds
    # as the sync /transcribe worker path, not hardcoded module constants.
    s = Settings(
        no_speech_threshold=0.5,
        log_prob_threshold=-2.0,
        compression_ratio_threshold=3.0,
        condition_on_previous_text=True,
    )
    for service in (get_live_service(s), get_final_service(s)):
        assert service.no_speech_threshold == 0.5
        assert service.log_prob_threshold == -2.0
        assert service.compression_ratio_threshold == 3.0
        assert service.condition_on_previous_text is True


def test_transcribe_array_passes_configured_decode_thresholds_to_model() -> None:
    service = DirectWhisperService(
        model_name="test-model",
        device="cpu",
        compute_type="int8",
        language="tr",
        beam_size=1,
        no_speech_threshold=0.5,
        log_prob_threshold=-2.0,
        compression_ratio_threshold=3.0,
        condition_on_previous_text=True,
    )

    class FakeModel:
        kwargs: dict[str, object] | None = None

        def transcribe(self, _audio: object, **kwargs: object) -> tuple[list[object], object]:
            self.kwargs = kwargs
            return [SimpleNamespace(text="Merhaba", no_speech_prob=0.1, avg_logprob=-0.2)], object()

    fake_model = FakeModel()
    service._model = fake_model

    service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=False)

    assert fake_model.kwargs is not None
    assert fake_model.kwargs["no_speech_threshold"] == 0.5
    assert fake_model.kwargs["log_prob_threshold"] == -2.0
    assert fake_model.kwargs["compression_ratio_threshold"] == 3.0
    assert fake_model.kwargs["condition_on_previous_text"] is True


def test_raising_no_speech_threshold_keeps_segment_default_would_drop() -> None:
    # A segment at no_speech_prob 0.8 is dropped at the default 0.75 threshold
    # but kept once an operator raises the threshold to 0.9 — proving the
    # post-decode filter honors the configured value, not a constant.
    def _service(no_speech_threshold: float) -> DirectWhisperService:
        svc = DirectWhisperService(
            model_name="test-model",
            device="cpu",
            compute_type="int8",
            language="tr",
            beam_size=1,
            no_speech_threshold=no_speech_threshold,
        )

        class FakeModel:
            def transcribe(self, _a: object, **_k: object) -> tuple[list[object], object]:
                return [
                    SimpleNamespace(text="Devam edelim.", no_speech_prob=0.8, avg_logprob=-0.2)
                ], object()

        svc._model = FakeModel()
        return svc

    dropped = _service(0.75).transcribe_array(np.zeros(1600, dtype=np.float32), vad=False)
    kept = _service(0.9).transcribe_array(np.zeros(1600, dtype=np.float32), vad=False)
    assert dropped == ""
    assert kept == "Devam edelim."


def test_stream_router_importable_without_gpu() -> None:
    from app.api.stream import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert "/ws/stream" in paths


# ── #279: a failing draft must not take the connection down ──────────────────


def test_draft_timeout_keeps_stream_alive_and_opens_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slow draft must not end the session; repeated ones must not storm.

    Before the fix the supervised branch re-raised, so a single draft timeout
    under GPU contention closed the WebSocket — losing audio the final worker
    could still transcribe. After `DRAFT_FAILURES_BEFORE_CIRCUIT_OPEN`
    consecutive failures the draft is skipped outright for a cooldown, so the
    kill/reload cycle cannot be re-entered at the 700ms draft cadence.
    """
    settings = Settings(
        stream_live_worker_backend="inline",
        stream_final_worker_backend="inline",
        stream_preload_models=False,
        live_infer_interval_ms=1,
        min_infer_sec=0.01,
        live_window_sec=0.5,
        silence_rms=0.0001,
        min_speech_rms=0.0001,
        stream_debug=True,
    )

    draft_attempts = 0

    class AlwaysTimingOutLive:
        hard_timeout = True
        ready_generation = 1

        def ensure_model(self, **_kwargs: object) -> None:
            return None

        def transcribe_array(self, *_args: object, **_kwargs: object) -> str:
            nonlocal draft_attempts
            draft_attempts += 1
            raise WorkerTimeoutError("streaming live worker exceeded timeout")

        def transcribe_loaded_array(self, *_args: object, **_kwargs: object) -> str:
            return self.transcribe_array()

    class HealthyFinal:
        hard_timeout = False
        ready_generation = 1

        def ensure_model(self, **_kwargs: object) -> None:
            return None

        def transcribe_array(self, *_args: object, **_kwargs: object) -> str:
            return "Geçiş ülkelerinde yaşananlar ise karışık."

        def transcribe_loaded_array(self, *_args: object, **_kwargs: object) -> str:
            return self.transcribe_array()

    monkeypatch.setattr(stream_api, "get_live_service", lambda _s: AlwaysTimingOutLive())
    monkeypatch.setattr(stream_api, "get_final_service", lambda _s: HealthyFinal())

    speech = (np.ones(8_000, dtype=np.float32) * 0.05).tobytes()
    frames = [{"bytes": speech} for _ in range(40)]

    class FakeWebSocket:
        def __init__(self) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(streaming_preload=None))
            self.query_params = {"protocol": stream_api.STREAM_PROTOCOL}
            self.events: list[dict[str, object]] = []
            self.close_code: int | None = None
            self._pending = list(frames)

        async def accept(self) -> None:
            return None

        async def send_json(self, event: dict[str, object]) -> None:
            self.events.append(event)

        async def receive(self) -> dict[str, object]:
            await asyncio.sleep(0.01)
            if self._pending:
                return self._pending.pop(0)
            return {"type": "websocket.disconnect"}

        async def close(self, *, code: int = 1000) -> None:
            self.close_code = code

    websocket = FakeWebSocket()
    asyncio.run(stream_api.stream_endpoint(websocket, settings))  # type: ignore[arg-type]

    kinds = [event.get("type") for event in websocket.events]
    assert "ready" in kinds, websocket.events[:3]
    # The connection must never be failed by a draft-only fault.
    assert "error" not in kinds, [e for e in websocket.events if e.get("type") == "error"]

    debug_events = [e.get("event") for e in websocket.events if e.get("type") == "debug"]
    assert "draft_circuit_opened" in debug_events, debug_events
    assert "draft_circuit_open" in debug_events, debug_events

    # The circuit must actually stop the retries, not merely log them.
    assert draft_attempts == stream_api.DRAFT_FAILURES_BEFORE_CIRCUIT_OPEN, draft_attempts
