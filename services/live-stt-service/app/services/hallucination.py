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


def is_hallucination(text: str) -> bool:
    """True when the candidate transcript should be suppressed."""
    normalized = (text or "").strip()
    if not normalized:
        return True
    if len(normalized) < 3:
        return True
    return any(p.fullmatch(normalized) for p in _HALLUCINATION_PATTERNS)
