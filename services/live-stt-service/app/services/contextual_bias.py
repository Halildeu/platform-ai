"""Bounded, request-scoped vocabulary hints for Whisper decoding.

Context terms may contain participant or domain names, so callers must keep the
normalized values out of logs and durable evidence. This module validates only;
it does not persist or cache the supplied terms.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

MAX_CONTEXT_TERMS = 32
MAX_CONTEXT_TERM_CHARS = 64
MAX_CONTEXT_TOTAL_CHARS = 512
_ALLOWED_PUNCTUATION = frozenset({" ", "-", "'", "."})


class ContextualBiasError(ValueError):
    """The client supplied an unsafe or unbounded contextual-bias payload."""


@dataclass(frozen=True)
class ContextualBias:
    terms: tuple[str, ...]

    @property
    def hotwords(self) -> str | None:
        return ", ".join(self.terms) if self.terms else None


def _normalize_term(value: str) -> str:
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ContextualBiasError("context term contains control characters")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ContextualBiasError("context term must not be blank")
    if len(normalized) > MAX_CONTEXT_TERM_CHARS:
        raise ContextualBiasError("context term exceeds character limit")
    for character in normalized:
        category = unicodedata.category(character)
        if category[0] not in {"L", "M", "N"} and character not in _ALLOWED_PUNCTUATION:
            raise ContextualBiasError("context term contains unsupported characters")
    return normalized


def normalize_context_terms(values: Sequence[object]) -> ContextualBias:
    """Normalize and deduplicate untrusted context without retaining raw input."""
    if isinstance(values, str | bytes) or len(values) > MAX_CONTEXT_TERMS:
        raise ContextualBiasError("context terms must be a bounded list")

    terms: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for value in values:
        if not isinstance(value, str):
            raise ContextualBiasError("context term must be a string")
        term = _normalize_term(value)
        key = term.casefold()
        if key in seen:
            continue
        total_chars += len(term)
        if total_chars > MAX_CONTEXT_TOTAL_CHARS:
            raise ContextualBiasError("context terms exceed total character limit")
        seen.add(key)
        terms.append(term)

    return ContextualBias(tuple(terms))
