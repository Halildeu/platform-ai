"""Word-level transcript diff for UI revision rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

_TOKEN_RE = re.compile(r"\S+")


class DiffOperation(StrEnum):
    EQUAL = "equal"
    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"


@dataclass(frozen=True)
class TranscriptDiff:
    operation: DiffOperation
    before_start: int
    before_end: int
    after_start: int
    after_end: int
    before_text: str
    after_text: str


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.strip())


def _normalise(token: str) -> str:
    return token.casefold().strip(".,!?;:\"'()[]{}")


def diff_transcripts(before: str, after: str) -> list[TranscriptDiff]:
    """Return stable word-indexed operations from draft text to replacement text."""
    before_tokens = _tokens(before)
    after_tokens = _tokens(after)
    matcher = SequenceMatcher(
        None,
        [_normalise(token) for token in before_tokens],
        [_normalise(token) for token in after_tokens],
        autojunk=False,
    )
    return [
        TranscriptDiff(
            operation=DiffOperation(tag),
            before_start=before_start,
            before_end=before_end,
            after_start=after_start,
            after_end=after_end,
            before_text=" ".join(before_tokens[before_start:before_end]),
            after_text=" ".join(after_tokens[after_start:after_end]),
        )
        for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes()
    ]
