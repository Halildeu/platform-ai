"""#128 streaming port tests — GPU-free (no model load)."""

# ruff: noqa: RUF001 - intentional Turkish strings in fixtures.

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.api import stream as stream_api
from app.api.stream import (
    _append_recent_final_text,
    _drop_leading_tail_overlap,
    _merge_final_transcript,
    _merge_rolling_partial,
    _select_commit_text,
    _select_partial_parts,
    _select_partial_text,
    _stabilize_rolling_partial,
)
from app.core import config as config_module
from app.core.config import Settings
from app.services.hallucination import is_hallucination
from app.services.streaming_models import (
    DirectWhisperService,
    SupervisedFinalWhisperService,
    SupervisedLiveWhisperService,
    get_final_service,
    get_live_service,
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
    assert s.stream_final_vad_filter is False
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
        service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=True)
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

    service.transcribe_array(np.zeros(1600, dtype=np.float32), vad=True)

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

    dropped = _service(0.75).transcribe_array(np.zeros(1600, dtype=np.float32), vad=True)
    kept = _service(0.9).transcribe_array(np.zeros(1600, dtype=np.float32), vad=True)
    assert dropped == ""
    assert kept == "Devam edelim."


def test_stream_router_importable_without_gpu() -> None:
    from app.api.stream import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert "/ws/stream" in paths
