"""Final transcript revision state machine and UI event contract."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from app.models.schemas import (
    FinalSttJob,
    FinalSttResult,
    TranscriptDiffOperation,
    TranscriptRevisionEvent,
)
from app.services.diff import diff_transcripts
from app.services.merge import merge_transcripts


class RevisionState(StrEnum):
    DRAFT = "draft"
    STABILIZING = "stabilizing"
    FINAL = "final"
    REVISED = "revised"


class InvalidStateTransitionError(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[RevisionState | None, RevisionState] = {
    None: RevisionState.DRAFT,
    RevisionState.DRAFT: RevisionState.STABILIZING,
    RevisionState.STABILIZING: RevisionState.FINAL,
    RevisionState.FINAL: RevisionState.REVISED,
}


class TranscriptRevisionStateMachine:
    def __init__(self, job: FinalSttJob) -> None:
        self._job = job
        self._state: RevisionState | None = None
        self._sequence = -1
        identity = f"{job.session_id}\0{job.chunk_seq}\0{job.correlation_id}".encode()
        self._revision_id = hashlib.sha256(identity).hexdigest()

    @property
    def state(self) -> RevisionState | None:
        return self._state

    @property
    def revision_id(self) -> str:
        return self._revision_id

    def draft(self) -> TranscriptRevisionEvent:
        return self._transition(
            RevisionState.DRAFT,
            text=self._job.draft_text,
            previous_text="",
            overlap_words=0,
        )

    def stabilizing(self) -> TranscriptRevisionEvent:
        return self._transition(
            RevisionState.STABILIZING,
            text=self._job.draft_text,
            previous_text=self._job.draft_text,
            overlap_words=0,
        )

    def final(self, result: FinalSttResult) -> TranscriptRevisionEvent:
        return self._transition(
            RevisionState.FINAL,
            text=result.final_chunk_text,
            previous_text=self._job.draft_text,
            overlap_words=0,
        )

    def revised(self, result: FinalSttResult) -> TranscriptRevisionEvent:
        displayed_draft = merge_transcripts(
            self._job.committed_text,
            self._job.draft_text,
        ).text
        return self._transition(
            RevisionState.REVISED,
            text=result.revised_text,
            previous_text=displayed_draft,
            overlap_words=result.overlap_words,
            result=result,
        )

    def _transition(
        self,
        target: RevisionState,
        *,
        text: str,
        previous_text: str,
        overlap_words: int,
        result: FinalSttResult | None = None,
    ) -> TranscriptRevisionEvent:
        expected = _ALLOWED_TRANSITIONS.get(self._state)
        if expected != target:
            current = self._state.value if self._state is not None else "initial"
            raise InvalidStateTransitionError(f"cannot transition from {current} to {target.value}")

        self._state = target
        self._sequence += 1
        diff = [
            TranscriptDiffOperation(
                operation=item.operation.value,
                beforeStart=item.before_start,
                beforeEnd=item.before_end,
                afterStart=item.after_start,
                afterEnd=item.after_end,
                beforeText=item.before_text,
                afterText=item.after_text,
            )
            for item in diff_transcripts(previous_text, text)
        ]
        return TranscriptRevisionEvent(
            sessionId=self._job.session_id,
            chunkSeq=self._job.chunk_seq,
            correlationId=self._job.correlation_id,
            revisionId=self._revision_id,
            state=target.value,
            stateSequence=self._sequence,
            terminal=target == RevisionState.REVISED,
            text=text,
            previousText=previous_text,
            overlapWords=overlap_words,
            diff=diff,
            result=result,
        )
