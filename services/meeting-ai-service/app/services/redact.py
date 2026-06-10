"""PII redaction applied to a transcript BEFORE any analyzer/LLM call.

KVKK boundary (ADR-0030): a transcript is sensitive. The meeting-ai service
redacts bearer tokens, secrets, emails, Turkish national IDs (TC), IBANs and
Turkish phone numbers from the text before the analyzer — mock or a future real
LLM — ever sees it. Pure function, unit-tested.
"""

from __future__ import annotations

import re

# Patterns mirror live-stt's log redaction set (Codex rev 019e8846).
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)bearer[\s:=]+[A-Za-z0-9\-_]+\.?[A-Za-z0-9\-_.]+"), "***REDACTED***"),
    (re.compile(r"(?i)(secret|password|token)[\s:=]+\S+"), "***REDACTED***"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "***REDACTED_EMAIL***"),
    # TC kimlik: 11 digits, first 1-9
    (re.compile(r"\b[1-9]\d{10}\b"), "***REDACTED_TC***"),
    # IBAN TR: TR + 24 digits
    (re.compile(r"\bTR\d{24}\b"), "***REDACTED_IBAN***"),
    # Turkish mobile: optional +90/0 prefix, 5xx group
    (re.compile(r"\b(\+90|0)?[\s]?5\d{2}[\s]?\d{3}[\s]?\d{2}[\s]?\d{2}\b"), "***REDACTED_PHONE***"),
]


def redact_pii(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redacted_spans)."""
    count = 0
    result = text
    for pattern, replacement in _REDACT_PATTERNS:
        result, n = pattern.subn(replacement, result)
        count += n
    return result, count
