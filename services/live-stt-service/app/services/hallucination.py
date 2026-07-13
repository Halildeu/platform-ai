"""Whisper hallucination filter (#128).

Empty, structurally repeated, or narrowly identified decoder artefacts are
rejected before they reach the client. Ordinary meeting phrases are not
suppressed solely by their text.
"""

# ruff: noqa: RUF001 - Turkish dotless-i inside regex character classes is the point.

from __future__ import annotations

import re

_HALLUCINATION_PATTERNS = [
    re.compile(r".*videoyu be[gğ]enmeyi.*unutmay[iı]n.*", re.IGNORECASE),
    re.compile(r".*bir sonraki videoda g[oö]r[uü][sş][uü]r[uü]z.*", re.IGNORECASE),
    re.compile(r".*[cç]eviri.*videoyu.*", re.IGNORECASE),
    re.compile(r"^altyaz[iı]\s+[a-zçğıöşü](?:\.[a-zçğıöşü])?\.?$", re.IGNORECASE),
    re.compile(r"^abone ol.*", re.IGNORECASE),
    re.compile(r"^(cis|ces)[.!]?$", re.IGNORECASE),
    re.compile(r"^neroba[.!]?$", re.IGNORECASE),
    re.compile(r"^[.!?]*$", re.IGNORECASE),
]

# Valid short Turkish utterances that the generic `len < 3` artifact guard
# would otherwise drop (#238). Real meetings contain short acknowledgements and
# clarification responses ("Ne?", "Ha?", "He.", "Yok."); suppressing them makes
# the product feel like it swallows small but meaningful speech. Matched after
# casefold + stripping trailing punctuation/whitespace, so the punctuated forms
# are covered too. Kept deliberately narrow to interjections/yes-no/clarifiers
# so it never re-admits the short artifact finals the guard exists for
# (punctuation-only "..", "cis", "neroba" — none of which appear here).
_SHORT_UTTERANCE_ALLOWLIST = frozenset(
    {
        "ne",
        "ha",
        "he",
        "hı",
        "hah",
        "ya",
        "yo",
        "of",
        "eh",
        "hmm",
        "evet",
        "yok",
        "olur",
        "peki",
        "tamam",
        "hayır",
    }
)
_ALLOWLIST_TRIM = " \t.,!?…"

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


def _normalized_families(text: str) -> list[str]:
    return [_word_family(word) for word in _normalized_words(text)]


def _shared_token_ratio(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    denominator = min(len(left_set), len(right_set))
    if denominator == 0:
        return 0.0
    return len(left_set & right_set) / denominator


def _repeated_family_count(families: list[str]) -> int:
    if not families:
        return 0
    return max(families.count(family) for family in set(families))


def _dominant_family_count_by_fragment(fragments: list[list[str]], dominant_family: str) -> int:
    return sum(1 for fragment in fragments if dominant_family in fragment)


def _sentence_fragments(text: str) -> list[list[str]]:
    fragments: list[list[str]] = []
    for fragment in re.split(r"[.!?…]+|\b(?:ya|yani)\b", text, flags=re.IGNORECASE):
        words = _normalized_words(fragment)
        if len(words) >= 2:
            fragments.append([_word_family(word) for word in words])
    return fragments


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


def _is_short_repeated_decode_chain(text: str) -> bool:
    """Catch compact rolling-window alternatives before they reach 8 words."""
    families = _normalized_families(text)
    if len(families) < 6 or len(families) >= 8:
        return False

    if len(set(families)) / len(families) > 0.75:
        return False

    bigrams = [tuple(families[index : index + 2]) for index in range(len(families) - 1)]
    return any(bigrams.count(ngram) >= 2 for ngram in set(bigrams))


def _is_repeated_alternative_chain(text: str) -> bool:
    """Reject sequences of near-duplicate ASR alternatives in one final payload."""
    families = _normalized_families(text)
    top_family_count = _repeated_family_count(families)
    if len(families) < 8 or top_family_count < 3:
        return False

    fragments = _sentence_fragments(text)
    if len(fragments) < 2:
        return False

    similar_pairs = 0
    for left_index, left in enumerate(fragments[:-1]):
        for right in fragments[left_index + 1 :]:
            if _shared_token_ratio(left, right) >= 0.6:
                similar_pairs += 1
            if similar_pairs >= (2 if len(fragments) >= 3 else 1):
                return True

    counts = {family: families.count(family) for family in set(families)}
    dominant = max(counts.items(), key=lambda item: item[1], default=("", 0))[0]
    return bool(
        dominant
        and top_family_count >= 4
        and _dominant_family_count_by_fragment(fragments, dominant) >= 2
        and any(len(fragment) <= 4 for fragment in fragments)
    )


def _is_allowlisted_short_utterance(normalized: str) -> bool:
    """True for a curated valid short Turkish utterance (#238).

    Checked before the `len < 3` artifact guard so genuine short responses
    survive. A multi-word string never matches (the allowlist holds single
    tokens), so it cannot rescue repeated-decode loops.
    """
    return normalized.casefold().strip(_ALLOWLIST_TRIM) in _SHORT_UTTERANCE_ALLOWLIST


def is_hallucination(text: str) -> bool:
    """True when the candidate transcript should be suppressed.

    Short-utterance handling (#238): the `len < 3` rule below is a deliberate
    artifact guard, but it would also drop valid short Turkish responses. Those
    are rescued first via `_SHORT_UTTERANCE_ALLOWLIST`; everything else shorter
    than 3 chars stays suppressed as a documented product tradeoff.
    """
    normalized = (text or "").strip()
    if not normalized:
        return True
    if _is_allowlisted_short_utterance(normalized):
        return False
    if len(normalized) < 3:
        return True
    if _is_low_information_repetition(normalized):
        return True
    if _is_short_repeated_decode_chain(normalized):
        return True
    if _is_repeated_alternative_chain(normalized):
        return True
    return any(p.fullmatch(normalized) for p in _HALLUCINATION_PATTERNS)
