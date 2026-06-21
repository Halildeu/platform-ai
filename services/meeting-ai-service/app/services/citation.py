"""#162 (T-C) citation + hallucination guard — ADR-0043 D4 hardened.

The product wedge for regulated use: every decision/action must be traceable to
a transcript sentence — "şu cümleden çıkarıldı", not "AI dedi", AND the claim must
be **entailed** by that sentence, not merely lexically overlapping.

Verification ladder (ADR-0043 D4, Codex 019ee7c9 — substring/overlap ≠ entailment):
1. **Content-word coverage** (necessary): how much of the claim's meaning is in the
   sentence (overlap coefficient). Below `threshold` → FAILED (ungrounded).
2. **Polarity/negation consistency** (the key entailment fix): a claim and its
   evidence must agree on affirmation vs rejection. "Bütçe reddedildi" cited to
   "Bütçe onaylandı" has high overlap but OPPOSITE meaning → FAILED.
3. **Evidence informativeness**: a generic span ("Tamam.", "Evet.") can't ground a
   decision/action → LOW_CONFIDENCE.
Only `PASSED` is `grounded=True` (shippable; ADR-0043 D8.1). Each citation carries a
hash/offset key (`source_char_start/end`, `source_hash`, `quote_hash`) so a verifier
can pin it to the exact transcript span (tamper/version guard).

Pure-Python, deterministic, CPU-unit-testable (no LLM/embeddings). A heavy NLI model
(SummaC/AlignScore-class) is a future upgrade behind the same interface; the polarity
check approximates entailment cheaply for the high-risk failure mode.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"\w+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# Turkish stop-ish words that add overlap noise without anchoring meaning.
_STOP = frozenset(
    {"ve", "ile", "bir", "bu", "şu", "o", "da", "de", "ki", "için", "olarak", "the", "a", "an"}
)
# Signed-outcome polarity (Codex 019ee9a6): a claim and its evidence must agree on
# whether the asserted thing HAPPENED (+1) or did NOT (-1). Modelling "did it happen"
# directly — via word FORMS that bake the Turkish negation suffix into the token —
# avoids the double-negation false-pass that a flat "has-any-negation-cue" check has
# ("iptal edildi" cancelled vs "iptal edilmedi" NOT cancelled both look "negated").
# Check the negated forms FIRST (they contain the affirmed substring as a prefix).
_NEG_POSITIVE = (  # negated positive outcome → effectively NEGATIVE (-1)
    "onaylanmadı",
    "onaylanmamış",
    "onaylanmayacak",
    "kabul edilmedi",
    "kabul etmedi",
    "kararlaştırılmadı",
    "tamamlanmadı",
    "imzalanmadı",
    "yapılmadı",
    "yapılmayacak",
)
_NEG_NEGATIVE = (  # negated negative outcome → effectively POSITIVE (+1)
    "reddedilmedi",
    "iptal edilmedi",
    "vazgeçilmedi",
    "ertelenmedi",
)
_POS_OUTCOME = (  # affirmed positive outcome (+1)
    "onaylandı",
    "onaylanmış",
    "kabul edildi",
    "mutabık",
    "kararlaştırıldı",
    "karara bağlandı",
    "tamamlandı",
    "gerçekleşti",
    "imzalandı",
    "kesinleşti",
    "yapılacak",
    "yapıldı",
)
_NEG_OUTCOME = (  # affirmed negative outcome (-1)
    "reddedildi",
    "reddedilmiş",
    "reddetti",
    "iptal edildi",
    "iptal oldu",
    "iptal",
    "vazgeçildi",
    "ertelendi",
    "reject",
    "declin",
    "cancel",
)
_GRAMMATICAL_NEGATION = (  # negation without an outcome word (-1)
    "değil",
    "gelmedi",
    "başlamadı",
    "katılmadı",
    "katılmıyor",
    "ödenmedi",
    "ödenmeyecek",
    "almadı",
    "verilmedi",
    "bitmedi",
    "olmadı",
    "olmayacak",
    "hayır",
)

_CITATION_VERIFIER_VERSION = "v3-adr0043-signed-polarity"
_DEFAULT_THRESHOLD = 0.4
_MIN_EVIDENCE_CONTENT_TOKENS = 2


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CitationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


@dataclass(frozen=True)
class Sentence:
    """One transcript sentence with its character span (for timestamp + hash pin)."""

    index: int
    text: str
    start_char: int
    end_char: int
    start_sec: float | None = None  # wall-clock start when STT timing is provided


@dataclass(frozen=True)
class Citation:
    """A claim grounded + entailment-checked against its source transcript sentence."""

    claim: str
    source_index: int  # -1 when ungrounded
    source_text: str
    similarity: float
    grounded: bool  # True iff status == PASSED (ADR-0043 D8.1 shippable)
    status: CitationStatus
    reason: str
    start_sec: float | None = None
    # ADR-0043 D4 hash/offset key (pin to the exact transcript span; "" when ungrounded)
    source_char_start: int = -1
    source_char_end: int = -1
    source_hash: str = ""
    quote_hash: str = ""


def _segment_spans(
    transcript: str, segments: list[dict[str, object]]
) -> list[tuple[int, float]] | None:
    """Locate each STT segment in the transcript → (start_char, start_sec), sorted.

    Fail-closed: if any non-empty segment cannot be located (redaction/whitespace
    drift), return None so NO citation gets a wrong timestamp.
    """
    spans: list[tuple[int, float]] = []
    pos = 0
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        loc = transcript.find(text, pos)
        if loc < 0:
            return None
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
    """Split into sentences, keeping each one's char offset in the transcript."""
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
    """Normalized Turkish content tokens (casefold, NFKC, drop stops + 1-char)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return {t for t in _WORD.findall(folded) if t not in _STOP and len(t) > 1}


def _similarity(claim_tokens: set[str], sent_tokens: set[str]) -> float:
    """Overlap coefficient: |A∩B| / |A| — how much of the claim is covered."""
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & sent_tokens) / len(claim_tokens)


def _polarity(text: str) -> int:
    """Signed outcome polarity: +1 (asserts it happened), -1 (asserts it did NOT), 0.

    Negated forms are checked BEFORE affirmed ones so "iptal edilmedi" resolves to +1
    (not cancelled) rather than matching "iptal" (-1). An outcome word, when present,
    decides polarity; bare grammatical negation only applies when no outcome word is.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    if any(cue in folded for cue in _NEG_POSITIVE):
        return -1
    if any(cue in folded for cue in _NEG_NEGATIVE):
        return 1
    if any(cue in folded for cue in _NEG_OUTCOME):
        return -1
    if any(cue in folded for cue in _POS_OUTCOME):
        return 1
    if any(cue in folded for cue in _GRAMMATICAL_NEGATION):
        return -1
    return 0


def _numbers(text: str) -> set[str]:
    """Digit groups (incl. decimals/percent), normalized for equality gating.

    Turkish writes "%20" / "20%" / "yüzde 20" — only the digit run matters for the
    equality check; the decimal comma is normalized to a dot.
    """
    return {m.group().replace(",", ".") for m in _NUMBER.finditer(text)}


def _ungrounded(claim: str, similarity: float, reason: str) -> Citation:
    return Citation(
        claim=claim,
        source_index=-1,
        source_text="",
        similarity=round(similarity, 3),
        grounded=False,
        status=CitationStatus.FAILED,
        reason=reason,
        start_sec=None,
    )


def ground_claim(
    claim: str, sentences: list[Sentence], threshold: float = _DEFAULT_THRESHOLD
) -> Citation:
    """Best-matching sentence + entailment check (ADR-0043 D4).

    PASSED → grounded; FAILED/LOW_CONFIDENCE → not grounded (not shippable, D8.1).
    """
    ctoks = _tokens(claim)
    best: Sentence | None = None
    best_sim = 0.0
    for s in sentences:
        sim = _similarity(ctoks, _tokens(s.text))
        if sim > best_sim:
            best, best_sim = s, sim

    if best is None or best_sim < threshold:
        return _ungrounded(claim, best_sim, "no transcript sentence covers the claim")

    # Coverage met → hard acceptance gates (high-precision; ADR-0043 D4 + Codex 019ee9a6:
    # lexical overlap / embedding cosine BOTH miss these, so they gate AFTER retrieval).
    # GATE — signed-outcome polarity: opposite non-zero signs = contradiction, NOT support
    # (catches "iptal edildi" vs "iptal edilmedi", "onaylandı" vs "reddedildi/onaylanmadı").
    if _polarity(claim) * _polarity(best.text) < 0:
        return _ungrounded(claim, best_sim, "polarity/negation contradiction with source")

    # GATE — number/quantity equality: every number/percent/date-digit in the claim
    # must appear in the source span (catches "%2"→"%20" swaps that overlap misses).
    claim_nums = _numbers(claim)
    if claim_nums and not claim_nums.issubset(_numbers(best.text)):
        return _ungrounded(claim, best_sim, "number/quantity in claim not found in source")

    evidence_informative = len(_tokens(best.text)) >= _MIN_EVIDENCE_CONTENT_TOKENS
    status = CitationStatus.PASSED if evidence_informative else CitationStatus.LOW_CONFIDENCE
    grounded = status == CitationStatus.PASSED
    reason = "entailed by source sentence" if grounded else "source span too generic to ground"

    return Citation(
        claim=claim,
        source_index=best.index if grounded else -1,
        source_text=best.text if grounded else "",
        similarity=round(best_sim, 3),
        grounded=grounded,
        status=status,
        reason=reason,
        start_sec=best.start_sec if grounded else None,
        source_char_start=best.start_char if grounded else -1,
        source_char_end=best.end_char if grounded else -1,
        source_hash=_sha256_hex(best.text) if grounded else "",
        quote_hash=_sha256_hex(best.text) if grounded else "",
    )


def ground_claims(
    claims: list[str],
    transcript: str,
    threshold: float = _DEFAULT_THRESHOLD,
    segments: list[dict[str, object]] | None = None,
) -> tuple[list[Citation], int]:
    """Ground + entailment-check every claim; return (citations, ungrounded_count)."""
    sentences = split_sentences(transcript, segments)
    citations = [ground_claim(c, sentences, threshold) for c in claims if c.strip()]
    ungrounded = sum(1 for c in citations if not c.grounded)
    return citations, ungrounded
