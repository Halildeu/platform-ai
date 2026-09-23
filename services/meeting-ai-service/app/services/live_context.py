"""Stateless incremental context. No transcript cache or per-user model state."""

from __future__ import annotations

import hashlib

from app.models.schemas import AnalyzeResponse, LiveAnalysisCursor
from app.services.citation import Sentence


def live_menu(
    transcript: str, sentences: list[Sentence], cursor: LiveAnalysisCursor | None
) -> list[Sentence]:
    """Revisit active claims and recent context together with ALL new sentences.

    A correction, rolling-window truncation or invalid cursor falls back to the
    whole source. Cursor indices never directly become decisions. The model must
    select them again, then the normal verifier grounds against the full source.
    """
    if cursor is None or cursor.source_length > len(transcript):
        return sentences
    prefix = transcript[: cursor.source_length]
    if hashlib.sha256(prefix.encode()).hexdigest() != cursor.source_sha256:
        return sentences
    if any(i < 0 or i >= len(sentences) for i in cursor.active_indices):
        return sentences
    # The preceding last sentence may have grown since the previous request.
    # Keep three old sentences, not just sentences starting after the cursor.
    old = [i for i, s in enumerate(sentences) if s.start_char < cursor.source_length]
    keep = set(cursor.active_indices) | set(old[-3:])
    keep.update(i for i, s in enumerate(sentences) if s.start_char >= cursor.source_length)
    return [sentence for i, sentence in enumerate(sentences) if i in keep]


def result_cursor(transcript: str, result: AnalyzeResponse) -> LiveAnalysisCursor:
    indices = sorted(
        {
            c.source_index
            # A summary can mention a cancelled task historically. Carrying
            # that source forward after its cancellation leaves recent context
            # could resurrect the task, so only active decision/action evidence
            # belongs in the incremental state. Live summaries use recent speech.
            for c in result.citations
            if c.grounded and c.source_index >= 0
        }
    )
    return LiveAnalysisCursor(
        source_length=len(transcript),
        source_sha256=hashlib.sha256(transcript.encode()).hexdigest(),
        active_indices=indices[:23],
    )
