"""gitops#3444 — Turkish morphology for citation grounding recall.

Why this module exists
======================

The grounding ladder in `citation.py` is precision-first: every content token of
a claim must appear in the single cited transcript sentence
(`_MAX_UNSUPPORTED_CONTENT_TOKENS = 0`). With EXACT surface tokens that bar is
brutal for Turkish — an agglutinative language where the LLM's paraphrase
("bütçenin onaylanması") and the transcript ("bütçeyi onayladık") share stems
but almost never share surface forms. Measured cost on k3d-test, 2026-08-05:
**22 of 76 analyses (29%) shipped with an empty, fully-withheld summary.**

`_similarity`'s own docstring names the sanctioned fix: "Suffix-aware recall (a
proper, tense-preserving Turkish lemmatizer) is the v2 roadmap, not a
heuristic." This module is that lemmatizer seam, built on Zeyrek — the Python
port of Zemberek, the de-facto standard Turkish morphological analyzer — kept
deterministic, CPU-only and unit-testable like the rest of the verifier.

The tense/aspect guard (why naive stemming was rejected)
========================================================

"onaylandı" (it WAS approved) and "onaylanması" (its approval — pending) share
the stem "onaylan"; merging them false-passes a pending item as decided. The
guard here is categorical, not heuristic: **a token that has ANY verb reading
contributes no lemmas** (`content_lemmas` returns an empty set), so verbs can
only ever match by exact surface form. Zeyrek's real output makes this safe:
"onaylandı" carries a Verb parse (→ exact-only), while "onaylanması" parses as
verbal nouns — but its counterpart being exact-only already blocks the pair.
What remains is the majority recall class that is semantically safe: nominal
case/possessive inflection ("bütçenin"/"bütçe", "raporda"/"rapor",
"tamamlanmasına"/"tamamlanması").

The short-lemma / compound guard
================================

Zeyrek analyses "canlı" with a `can+lı` reading, so lemma sets alone would let
an owner-ish token "can" ride on "canlı" — exactly the false attribution
`_phrase_present` warns about. Two structural guards close it:

* a shared lemma shorter than ``_MIN_SHARED_LEMMA_CHARS`` never matches;
* the shared lemma must be a **prefix of both surface forms** (true of Turkish
  suffixing morphology), tolerating only the standard final-consonant
  alternation (kitap→kitabı: p→b, ç→c, t→d, k→ğ/g).

Failure policy
==============

Zeyrek is optional at runtime. If it cannot be imported or a token cannot be
analyzed, `content_lemmas` returns an empty set and the caller degrades to the
pre-#3444 exact-surface behaviour — never an exception, never a looser match.
The availability transition is logged once so a host silently running in
fallback is observable.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache

logger = logging.getLogger(__name__)

# Precision guards for lemma-level matching (see module docstring).
_MIN_SHARED_LEMMA_CHARS = 4
# Standard Turkish final-consonant softening before vowel-initial suffixes.
_FINAL_SOFTENING = {"p": "b", "ç": "c", "t": "d", "k": "ğ"}
_FINAL_SOFTENING_ALT = {"k": "g"}  # renk → rengi

_analyzer = None
_analyzer_lock = threading.Lock()
_import_failed = False


def _get_analyzer():
    """Lazily build the shared Zeyrek analyzer; None when unavailable.

    Zeyrek's word-level `_parse` is deliberately used instead of the public
    `analyze(text)`: the latter routes through NLTK's punkt tokenizer, which
    demands a runtime data download — unacceptable on the air-gapped GPU host
    and irrelevant here because callers always pass single normalized tokens.
    The pin in requirements.txt plus the contract test in
    `test_citation_morphology.py` turn any future private-API drift into a red
    build instead of a silent recall regression.
    """
    global _analyzer, _import_failed
    if _analyzer is not None or _import_failed:
        return _analyzer
    with _analyzer_lock:
        if _analyzer is not None or _import_failed:
            return _analyzer
        try:
            from zeyrek import MorphAnalyzer

            # PII discipline: zeyrek logs every analysis — INCLUDING the
            # analyzed token, i.e. transcript content — and does so at
            # WARNING ("APPENDING RESULT: <(bütçe_Noun)...>"). A level bump is
            # not enough; cut propagation entirely so no transcript token can
            # ever reach the service log through this dependency.
            zeyrek_logger = logging.getLogger("zeyrek")
            zeyrek_logger.setLevel(logging.CRITICAL)
            zeyrek_logger.propagate = False
            zeyrek_logger.handlers = [logging.NullHandler()]
            _analyzer = MorphAnalyzer()
            logger.info("citation morphology active analyzer=zeyrek")
        except Exception as exc:  # pragma: no cover - exercised via fallback test
            _import_failed = True
            logger.warning(
                "citation morphology unavailable — exact-surface grounding only "
                "(err_class=%s)",
                type(exc).__name__,
            )
    return _analyzer


def available() -> bool:
    """Whether lemma-level matching is active on this host."""
    return _get_analyzer() is not None


@lru_cache(maxsize=16384)
def content_lemmas(token: str) -> frozenset[str]:
    """Casefolded lemmas for a NON-VERB token; empty set = exact-match only.

    Empty when: Zeyrek is unavailable, the token is unanalyzable, or any parse
    of the token is a verb (the tense/aspect guard — see module docstring).
    """
    analyzer = _get_analyzer()
    if analyzer is None:
        return frozenset()
    try:
        parses = analyzer._parse(token)
    except Exception:  # noqa: BLE001 - malformed input must degrade, not raise
        return frozenset()
    if not parses:
        return frozenset()
    lemmas: set[str] = set()
    for parse in parses:
        pos = getattr(parse, "pos", None)
        pos_name = getattr(pos, "name", str(pos))
        if pos_name == "Verb":
            return frozenset()
        lemma = getattr(getattr(parse, "dict_item", None), "lemma", None)
        if isinstance(lemma, str) and lemma:
            lemmas.add(lemma.casefold())
    return frozenset(lemmas)


def _lemma_prefixes_surface(lemma: str, surface: str) -> bool:
    """True when `surface` is `lemma` + suffixes (softening-tolerant).

    Turkish is exclusively suffixing, so a dictionary lemma must survive as the
    prefix of every inflected surface form — modulo the final-consonant
    alternation (kitap→kitabı). Anything else means Zeyrek reached the lemma
    through derivation (can+lı → "can"), which is exactly the compound case the
    matcher must NOT bridge.
    """
    if surface.startswith(lemma):
        return True
    if len(surface) <= len(lemma) - 1:
        return False
    stem, last = lemma[:-1], lemma[-1]
    if not surface.startswith(stem):
        return False
    surface_char = surface[len(stem)]
    return _FINAL_SOFTENING.get(last) == surface_char or (
        _FINAL_SOFTENING_ALT.get(last) == surface_char
    )


def tokens_share_nominal_lemma(claim_token: str, sentence_token: str) -> bool:
    """Guarded lemma match between two content tokens (see module docstring).

    True only when BOTH tokens are verb-free per `content_lemmas`, they share a
    lemma of at least `_MIN_SHARED_LEMMA_CHARS`, and that lemma is a
    (softening-tolerant) prefix of both surface forms.
    """
    if claim_token == sentence_token:
        return True
    claim_lemmas = content_lemmas(claim_token)
    if not claim_lemmas:
        return False
    sentence_lemmas = content_lemmas(sentence_token)
    if not sentence_lemmas:
        return False
    for lemma in claim_lemmas & sentence_lemmas:
        if len(lemma) < _MIN_SHARED_LEMMA_CHARS:
            continue
        if _lemma_prefixes_surface(lemma, claim_token) and _lemma_prefixes_surface(
            lemma, sentence_token
        ):
            return True
    return False
