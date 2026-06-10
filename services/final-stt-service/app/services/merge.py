"""Deterministic transcript overlap merge used before the #39 state machine."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class MergeResult:
    text: str
    overlap_words: int


def _normalise(token: str) -> str:
    return token.casefold().strip(".,!?;:\"'()[]{}")


def merge_transcripts(committed_text: str, revised_chunk: str) -> MergeResult:
    """Append a revised chunk while removing the longest word overlap."""
    committed = committed_text.strip()
    revised = revised_chunk.strip()
    if not committed:
        return MergeResult(text=revised, overlap_words=0)
    if not revised:
        return MergeResult(text=committed, overlap_words=0)

    committed_tokens = _TOKEN_RE.findall(committed)
    revised_tokens = _TOKEN_RE.findall(revised)
    max_overlap = min(len(committed_tokens), len(revised_tokens))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        left = [_normalise(token) for token in committed_tokens[-size:]]
        right = [_normalise(token) for token in revised_tokens[:size]]
        if left == right:
            overlap = size
            break

    suffix = " ".join(revised_tokens[overlap:])
    text = committed if not suffix else f"{committed} {suffix}"
    return MergeResult(text=text, overlap_words=overlap)
