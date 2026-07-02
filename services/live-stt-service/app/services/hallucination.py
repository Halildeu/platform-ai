"""Whisper hallucination filter (#128).

Short / empty / known-pattern outputs that Whisper emits on silence or music
(classic YouTube-caption artefacts, Turkish-specific set curated from the GPU
demo sessions) are rejected before they reach the client.
"""

# ruff: noqa: RUF001 - Turkish dotless-i inside regex character classes is the point.

from __future__ import annotations

import re

_HALLUCINATION_PATTERNS = [
    re.compile(r".*videoyu be[gğ]enmeyi.*unutmay[iı]n.*", re.IGNORECASE),
    re.compile(r".*bir sonraki videoda g[oö]r[uü][sş][uü]r[uü]z.*", re.IGNORECASE),
    re.compile(r".*[cç]eviri.*videoyu.*", re.IGNORECASE),
    re.compile(r"^altyaz[iı].*", re.IGNORECASE),
    re.compile(r"^abone ol.*", re.IGNORECASE),
    re.compile(r"^izledi[gğ]iniz i[cç]in te[sş]ekk[uü]r ederim[.!]?$", re.IGNORECASE),
    re.compile(r"^te[sş]ekk[uü]r ederim[.!]?$", re.IGNORECASE),
    re.compile(r"^te[sş]ekk[uü]rler[.!]?$", re.IGNORECASE),
    re.compile(r"^g[oö]r[uü][sş][uü]r[uü]z[.!]?$", re.IGNORECASE),
    re.compile(r"^iyi g[uü]nler[.!]?$", re.IGNORECASE),
    re.compile(r"^you know.*", re.IGNORECASE),
    re.compile(r".*thank you.*", re.IGNORECASE),
    re.compile(r"^my mom.*", re.IGNORECASE),
    re.compile(r"^(cis|ces)[.!]?$", re.IGNORECASE),
    re.compile(r"^[.!?]*$", re.IGNORECASE),
]

_WORD_RE = re.compile(r"[\wçğıöşüÇĞİÖŞÜ]+", re.UNICODE)
_SOFT_SUFFIXES = (
    "siniz",
    "sınız",
    "sunuz",
    "sünüz",
    "sin",
    "sın",
    "sun",
    "sün",
    "tim",
    "tım",
    "tum",
    "tüm",
    "dim",
    "dım",
    "dum",
    "düm",
)
_TRAILING_VOWELS = tuple("aeıioöuü")


def _normalized_words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD_RE.findall(text or "")]


def _word_family(word: str) -> str:
    family = word
    for suffix in _SOFT_SUFFIXES:
        if len(family) > len(suffix) + 3 and family.endswith(suffix):
            family = family[: -len(suffix)]
            break
    if len(family) >= 5 and family.endswith(_TRAILING_VOWELS):
        family = family[:-1]
    return family


def _is_low_information_repetition(text: str) -> bool:
    """Reject long repeated decode loops without logging transcript content.

    Whisper can turn a short uncertain phrase into several near-identical
    alternatives inside one final segment. A low unique-token ratio is a compact
    signal for that failure mode. The extra word-family pass catches Turkish
    suffix variants in the same bad segment without logging transcript content.
    """
    words = _normalized_words(text)
    if len(words) < 8:
        return False

    unique_ratio = len(set(words)) / len(words)
    if unique_ratio <= 0.45:
        return True

    families = [_word_family(word) for word in words]
    family_unique_ratio = len(set(families)) / len(families)
    if family_unique_ratio <= 0.5:
        return True

    ngram_sizes = (2,) if len(words) < 12 else (2, 3)
    for tokens in (words, families):
        for ngram_size in ngram_sizes:
            ngrams = [
                tuple(tokens[index : index + ngram_size])
                for index in range(len(tokens) - ngram_size + 1)
            ]
            if not ngrams:
                continue
            max_repeats = max(ngrams.count(ngram) for ngram in set(ngrams))
            if max_repeats >= 3:
                return True

    return False


def is_hallucination(text: str) -> bool:
    """True when the candidate transcript should be suppressed."""
    normalized = (text or "").strip()
    if not normalized:
        return True
    if len(normalized) < 3:
        return True
    if _is_low_information_repetition(normalized):
        return True
    return any(p.fullmatch(normalized) for p in _HALLUCINATION_PATTERNS)
