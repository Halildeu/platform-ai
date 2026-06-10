from __future__ import annotations

import pytest

from app.models.schemas import FinalSttJob, FinalSttResult
from app.services.state_machine import (
    InvalidStateTransitionError,
    RevisionState,
    TranscriptRevisionStateMachine,
)


def make_job() -> FinalSttJob:
    return FinalSttJob(
        sessionId="session-1",
        chunkSeq=8,
        audioPath="chunk.wav",
        audioDurationSec=12.0,
        committedText="Bugün toplantıda",
        draftText="karar alındı",
        correlationId="corr-1",
    )


def make_result() -> FinalSttResult:
    return FinalSttResult(
        sessionId="session-1",
        chunkSeq=8,
        correlationId="corr-1",
        revisedText="Bugün toplantıda karar alındı.",
        finalChunkText="karar alındı.",
        draftText="karar alındı",
        overlapWords=2,
        language="tr",
        languageProbability=0.99,
        audioDurationSec=12.0,
        elapsedMs=700,
        model="large-v3",
        computeType="float16",
        device="cuda",
        segments=[],
    )


def test_state_machine_emits_complete_ui_contract_in_order() -> None:
    machine = TranscriptRevisionStateMachine(make_job())
    result = make_result()

    events = [
        machine.draft(),
        machine.stabilizing(),
        machine.final(result),
        machine.revised(result),
    ]

    assert [event.state for event in events] == [
        RevisionState.DRAFT,
        RevisionState.STABILIZING,
        RevisionState.FINAL,
        RevisionState.REVISED,
    ]
    assert [event.state_sequence for event in events] == [0, 1, 2, 3]
    assert [event.terminal for event in events] == [False, False, False, True]
    assert len({event.revision_id for event in events}) == 1
    assert events[2].text == "karar alındı."
    assert events[2].result is None
    assert events[3].text == "Bugün toplantıda karar alındı."
    assert events[3].previous_text == "Bugün toplantıda karar alındı"
    assert events[3].overlap_words == 2
    assert events[3].result == result


def test_revision_id_is_deterministic_for_retry_deduplication() -> None:
    first = TranscriptRevisionStateMachine(make_job())
    second = TranscriptRevisionStateMachine(make_job())

    assert first.revision_id == second.revision_id


def test_invalid_transition_is_rejected() -> None:
    machine = TranscriptRevisionStateMachine(make_job())

    with pytest.raises(InvalidStateTransitionError, match="initial to stabilizing"):
        machine.stabilizing()


def test_repeated_state_is_rejected() -> None:
    machine = TranscriptRevisionStateMachine(make_job())
    machine.draft()

    with pytest.raises(InvalidStateTransitionError, match="draft to draft"):
        machine.draft()


def test_revised_cannot_skip_final_state() -> None:
    machine = TranscriptRevisionStateMachine(make_job())
    machine.draft()
    machine.stabilizing()

    with pytest.raises(InvalidStateTransitionError, match="stabilizing to revised"):
        machine.revised(make_result())
