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
    start_sec: float | None = None  # wall-clock start when STT timing is provided


@dataclass(frozen=True)
class Citation:
    """A claim grounded to its best-matching transcript sentence."""

    claim: str
    source_index: int  # -1 when ungrounded
    source_text: str
    similarity: float
    grounded: bool
    start_sec: float | None = None  # source sentence start (sec), None if no timing


def _segment_spans(
    transcript: str, segments: list[dict[str, object]]
) -> list[tuple[int, float]] | None:
    """Locate each STT segment in the transcript → (start_char, start_sec), sorted.

    Segments are Whisper-style dicts ({"text","start",...}). Fail-closed: if any
    non-empty segment cannot be located in the transcript (redaction/whitespace
    drift), return None so NO citation gets a wrong timestamp — better no stamp
    than a misleading one (review: silent mis-stamping).
    """
    spans: list[tuple[int, float]] = []
    pos = 0
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        loc = transcript.find(text, pos)
        if loc < 0:
            return None  # fail-closed: segments don't match transcript
        raw_start = seg.get("start", 0.0)
        start_sec = float(raw_start) if isinstance(raw_start, int | float | str) else 0.0
        spans.append((loc, start_sec))
        pos = loc + len(text)
    return spans


def _sec_for_char(start_char: int, spans: list[tuple[int, float]]) -> float | None:
    """Start-second of the last segment that begins at/before this char offset."""
    sec: float | None = None
    for seg_start_char, seg_sec in spans:
        if seg_start_char <= start_char:
            sec = seg_sec
        else:
            break
    return sec


def split_sentences(
    transcript: str, segments: list[dict[str, object]] | None = None
) -> list[Sentence]:
    """Split into sentences, keeping each one's char offset in the transcript.

    When `segments` (STT timing) is given, each sentence also carries the
    wall-clock `start_sec` of the segment its offset falls into.
    """
    spans = _segment_spans(transcript, segments) if segments else []
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
        out.append(
            Sentence(
                index=idx,
                text=s,
                start_char=start,
                end_char=end,
                start_sec=_sec_for_char(start, spans) if spans else None,
            )
        )
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
        start_sec=sentences[best_i].start_sec if grounded else None,
    )


def ground_claims(
    claims: list[str],
    transcript: str,
    threshold: float = 0.4,
    segments: list[dict[str, object]] | None = None,
) -> tuple[list[Citation], int]:
    """Ground every claim; return (citations, ungrounded_count) — the guard metric.

    Pass `segments` (STT timing) to attach a `start_sec` to each citation.
    """
    sentences = split_sentences(transcript, segments)
    citations = [ground_claim(c, sentences, threshold) for c in claims if c.strip()]
    ungrounded = sum(1 for c in citations if not c.grounded)
    return citations, ungrounded
