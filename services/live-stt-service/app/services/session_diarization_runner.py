"""Supervised post-session diarization runner (#3746 AI-D3, part 3).

Runs the whole-session pyannote batch in a spawned child process so a native
CUDA/model hang is killable (the diarization-service HTTP surface is not used:
its threadpool timeout cannot terminate inference — design D1/D4). The child
owns every heavy import (torch, pyannote) and its exit releases VRAM
deterministically, which is the cleanest possible ADR-0033 co-load guarantee:
no third resident model, ever.

Package boundary note: live-stt cannot import the diarization-service package
(separate service roots), so the child inlines the PR #348 adapter semantics —
seekable in-RAM ``BytesIO`` input, ``max_speakers`` forwarded, pinned model
revision — rather than importing it.

VRAM gate (ADR-0033): the child checks actually-free CUDA memory before
loading the model and reports ``DEFERRED_VRAM`` without loading anything when
the budget is not there; the caller retries with bounded backoff while live
decode / Ollama hold the GPU. The gate reads real free bytes, so the stale
"8 GB" ADR figure never mattered here.

ADR-0036: audio enters as bytes, is wrapped in RAM, and dies with the child.
Nothing is written to disk or logged; results are metadata only (labels and
milliseconds).
"""

from __future__ import annotations

import multiprocessing as mp
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing.connection import Connection
from typing import Any

from app.services.session_attribution import DiarTurn

__all__ = [
    "DiarizationOutcome",
    "DiarizationStatus",
    "RunnerConfig",
    "run_session_diarization",
]


class DiarizationStatus(Enum):
    DONE = "done"
    DEFERRED_VRAM = "deferred_vram"
    TIMEOUT_KILLED = "timeout_killed"
    FAILED = "failed"


@dataclass(frozen=True)
class RunnerConfig:
    """Batch parameters; every value is caller-supplied (settings), no globals."""

    model_name: str
    model_revision: str
    hf_token: str
    max_speakers: int
    device: str = "cuda"
    # Free-VRAM the child requires before it will load the model. ADR-0033
    # measured ~2.2 GB pipeline delta; the default adds headroom.
    required_free_vram_mb: int = 3000
    # Whole-batch wall cap. GPU RTF ~0.025 puts a 2 h session near 3 min;
    # the default is generous without approaching "hung forever".
    hard_timeout_sec: float = 900.0
    kill_grace_sec: float = 5.0

    def __post_init__(self) -> None:
        if not self.model_name or not self.model_revision:
            raise ValueError("model_name and model_revision are required")
        if not 1 <= self.max_speakers <= 50:
            raise ValueError("max_speakers must be 1..50")
        if self.required_free_vram_mb < 0:
            raise ValueError("required_free_vram_mb must be >= 0")
        if self.hard_timeout_sec <= 0 or self.kill_grace_sec < 0:
            raise ValueError("timeouts must be positive")


@dataclass(frozen=True)
class DiarizationOutcome:
    status: DiarizationStatus
    turns: list[DiarTurn] = field(default_factory=list)
    model_revision: str = ""
    elapsed_ms: int = 0
    # Non-content diagnostics only (exception class name, free-VRAM figure).
    detail: str = ""


def _child_main(  # pragma: no cover
    conn: Connection, wav_bytes: bytes, config: RunnerConfig
) -> None:
    """Child entry — heavy imports live here; exit releases VRAM.

    Excluded from coverage: unit tests exercise the parent supervision with a
    stub child (spawn + real pyannote needs the GPU host; that run is the D5
    measurement). Keep this function import-light at module level so tests
    never pay for torch.
    """
    try:
        if config.device == "cuda":
            import torch  # type: ignore[import-not-found]

            if not torch.cuda.is_available():
                conn.send(("failed", "cuda_unavailable", ""))
                return
            free_bytes, _total = torch.cuda.mem_get_info()
            free_mb = free_bytes // (1024 * 1024)
            if free_mb < config.required_free_vram_mb:
                conn.send(("deferred_vram", f"free_mb={free_mb}", ""))
                return
        from io import BytesIO

        import torch
        from pyannote.audio import Pipeline  # type: ignore[import-not-found]

        pipeline = Pipeline.from_pretrained(
            config.model_name,
            use_auth_token=config.hf_token,
            revision=config.model_revision,
        )
        if config.device == "cuda":
            pipeline.to(torch.device("cuda"))
        with BytesIO(wav_bytes) as audio:
            annotation = pipeline(audio, max_speakers=config.max_speakers)
        turns = [
            (label, int(segment.start * 1000), int(segment.end * 1000))
            for segment, _track, label in annotation.itertracks(yield_label=True)
            if segment.end > segment.start
        ]
        conn.send(("done", config.model_revision, turns))
    except Exception as exc:  # noqa: BLE001 - the child must always answer
        conn.send(("failed", type(exc).__name__, ""))
    finally:
        conn.close()


def run_session_diarization(
    wav_bytes: bytes,
    config: RunnerConfig,
    *,
    child_target: Callable[[Connection, bytes, RunnerConfig], None] = _child_main,
    mp_context: str = "spawn",
) -> DiarizationOutcome:
    """Run one supervised batch; never raises for runtime failures.

    ``child_target`` is injectable so unit tests pin the supervision protocol
    (timeout -> terminate -> kill, malformed replies, defer/fail paths)
    without touching torch. The default target is the real pyannote child.
    """
    if not wav_bytes:
        raise ValueError("wav_bytes must not be empty")
    ctx: Any = mp.get_context(mp_context)
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=child_target, args=(child_conn, wav_bytes, config), daemon=True)
    started = time.monotonic()
    process.start()
    child_conn.close()
    try:
        if not parent_conn.poll(config.hard_timeout_sec):
            process.terminate()
            process.join(config.kill_grace_sec)
            if process.is_alive():
                process.kill()
                process.join(config.kill_grace_sec)
            return DiarizationOutcome(
                status=DiarizationStatus.TIMEOUT_KILLED,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                detail="hard_timeout",
            )
        try:
            kind, detail, payload = parent_conn.recv()
        except (EOFError, ValueError, TypeError):
            return DiarizationOutcome(
                status=DiarizationStatus.FAILED,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                detail="child_protocol_error",
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if kind == "done":
            try:
                turns = [
                    DiarTurn(speaker=str(s), start_ms=int(a), end_ms=int(b)) for s, a, b in payload
                ]
            except (TypeError, ValueError):
                return DiarizationOutcome(
                    status=DiarizationStatus.FAILED,
                    elapsed_ms=elapsed_ms,
                    detail="child_turn_shape_error",
                )
            return DiarizationOutcome(
                status=DiarizationStatus.DONE,
                turns=turns,
                model_revision=str(detail),
                elapsed_ms=elapsed_ms,
            )
        if kind == "deferred_vram":
            return DiarizationOutcome(
                status=DiarizationStatus.DEFERRED_VRAM,
                elapsed_ms=elapsed_ms,
                detail=str(detail),
            )
        return DiarizationOutcome(
            status=DiarizationStatus.FAILED, elapsed_ms=elapsed_ms, detail=str(detail)
        )
    finally:
        parent_conn.close()
        process.join(config.kill_grace_sec)
        if process.is_alive():  # pragma: no cover - kill path exercised via timeout test
            process.kill()
            process.join(config.kill_grace_sec)
