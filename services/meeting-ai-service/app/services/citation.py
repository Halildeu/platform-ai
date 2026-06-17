"""#162 (T-C) citation + hallucination guard.

The product wedge for regulated use: every decision/action must be traceable to
a transcript sentence — "şu cümleden çıkarıldı", not "AI dedi". This module
grounds each LLM-produced claim against the (redacted) transcript:

- splits the transcript into sentences (with a running char offset → can map to
  a timestamp later when the transcript carries word timings);
- for each claim, finds the best-matching sentence by token overlap
  (normalized Turkish tokens — same spirit as live-stt's wer.normalize);
- flags a claim as **ungrounded (hallucination)** when its best overlap is below
  a threshold → the guard the LLM cannot satisfy by making things up.

Pure-Python, deterministic, CPU-unit-testable (no LLM/embeddings). The heavy LLM
step (Ollama) produces the claims; this module keeps them honest.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"\w+", re.UNICODE)
# Turkish stop-ish words that add overlap noise without anchoring meaning.
_STOP = frozenset(
    {"ve", "ile", "bir", "bu", "şu", "o", "da", "de", "ki", "için", "olarak", "the", "a", "an"}
)


@dataclass(frozen=True)
class Sentence:
    """One transcript sentence with its character span (for later timestamp map)."""

    index: int
    text: str
    start_char: int
    end_char: int


@dataclass(frozen=True)
class Citation:
    """A claim grounded to its best-matching transcript sentence."""

    claim: str
    source_index: int  # -1 when ungrounded
    source_text: str
    similarity: float
    grounded: bool


def split_sentences(transcript: str) -> list[Sentence]:
    """Split into sentences, keeping each one's char offset in the transcript."""
    out: list[Sentence] = []
    pos = 0
    idx = 0
    for raw in _SENTENCE_SPLIT.split(transcript):
        s = raw.strip()
        if not s:
            continue
        start = transcript.find(s, pos)
        if start < 0:
            start = pos
        end = start + len(s)
        out.append(Sentence(index=idx, text=s, start_char=start, end_char=end))
        pos = end
        idx += 1
    return out


def _tokens(text: str) -> set[str]:
    """Normalized Turkish content tokens (casefold, strip accents-noise, drop stops)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return {t for t in _WORD.findall(folded) if t not in _STOP and len(t) > 1}


def _similarity(claim_tokens: set[str], sent_tokens: set[str]) -> float:
    """Overlap coefficient: |A∩B| / |A| — how much of the claim is covered by the
    sentence. Robust when the sentence is longer than the claim (summaries)."""
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & sent_tokens) / len(claim_tokens)


def ground_claim(claim: str, sentences: list[Sentence], threshold: float = 0.4) -> Citation:
    """Best-matching sentence for a claim; ungrounded (hallucination) below threshold."""
    ctoks = _tokens(claim)
    best_i, best_sim = -1, 0.0
    for s in sentences:
        sim = _similarity(ctoks, _tokens(s.text))
        if sim > best_sim:
            best_i, best_sim = s.index, sim
    grounded = best_sim >= threshold and best_i >= 0
    return Citation(
        claim=claim,
        source_index=best_i if grounded else -1,
        source_text=sentences[best_i].text if grounded else "",
        similarity=round(best_sim, 3),
        grounded=grounded,
    )


def ground_claims(
    claims: list[str], transcript: str, threshold: float = 0.4
) -> tuple[list[Citation], int]:
    """Ground every claim; return (citations, ungrounded_count) — the guard metric."""
    sentences = split_sentences(transcript)
    citations = [ground_claim(c, sentences, threshold) for c in claims if c.strip()]
    ungrounded = sum(1 for c in citations if not c.grounded)
    return citations, ungrounded
