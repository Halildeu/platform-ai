"""PII redaction applied to a transcript BEFORE any analyzer/LLM call.

KVKK boundary (ADR-0030 + ADR-0043 D3): a transcript is sensitive. The meeting-ai
service redacts PII from the text before the analyzer — mock or a future real
LLM — ever sees it. Pure functions, unit-tested.

ADR-0043 D3 hardening:
- **Expanded precise patterns** (bearer/secret/email/TC/IBAN/phone + plate /
  passport / card).
- **Fail-closed residual gate** (`assert_no_residual_pii`): after best-effort
  redaction, a BROADER detector set re-scans the redacted text; if a likely-PII
  shape survived (a redaction gap from a formatting variant), it RAISES
  `RedactionError` so the caller does NOT send the text to an LLM. Better to fail
  the request than to leak PII (KVKK fail-closed). Error message is transcript-free.
"""

from __future__ import annotations

import re

# Versioned for provenance (ADR-0043 D3) — bump when the pattern set changes.
REDACTION_POLICY_VERSION = "v2-adr0043"


class RedactionError(RuntimeError):
    """Fail-closed: residual PII detected after redaction → MUST NOT reach an LLM.

    Message stays transcript-free (KVKK): only the detector label, never the value.
    """


# Precise redaction patterns (best-effort replace). Mirrors live-stt log redaction
# (Codex 019e8846) + ADR-0043 D3 expansion.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)bearer[\s:=]+[A-Za-z0-9\-_]+\.?[A-Za-z0-9\-_.]+"), "***REDACTED***"),
    (re.compile(r"(?i)(secret|password|token)[\s:=]+\S+"), "***REDACTED***"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "***REDACTED_EMAIL***"),
    # TC kimlik: 11 digits, first 1-9
    (re.compile(r"\b[1-9]\d{10}\b"), "***REDACTED_TC***"),
    # IBAN TR: TR + 24 digits (allow spaced grouping)
    (re.compile(r"\bTR\d{2}(?:[\s]?\d){22}\b"), "***REDACTED_IBAN***"),
    # Turkish mobile: optional +90/0 prefix, 5xx group
    (re.compile(r"\b(\+90|0)?[\s]?5\d{2}[\s]?\d{3}[\s]?\d{2}[\s]?\d{2}\b"), "***REDACTED_PHONE***"),
    # ADR-0043 D3: credit/debit card (13-16 digits, optional space/dash grouping)
    (re.compile(r"\b(?:\d[ -]?){12,15}\d\b"), "***REDACTED_CARD***"),
    # ADR-0043 D3: Turkish plate (e.g. "34 ABC 1234", "06 K 1234")
    (re.compile(r"\b\d{2}\s?[A-Za-z]{1,4}\s?\d{2,5}\b"), "***REDACTED_PLATE***"),
    # ADR-0043 D3: Turkish passport (1 letter + 8 digits, e.g. "U12345678")
    (re.compile(r"\b[A-Za-z]\d{8}\b"), "***REDACTED_PASSPORT***"),
    # ADR-0043 D3: context-gated VKN (vergi no 10 digits) — context avoids false-positives
    (re.compile(r"(?i)\bvergi\s*(?:no|numaras[ıi])?[\s:]*\d{10}\b"), "***REDACTED_VKN***"),
]

# Broader residual DETECTORS — catch what the precise set MISSED (formatting drift).
# Must NOT match the redaction placeholders (they carry no digits / no `@`).
_RESIDUAL_DETECTORS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{11}\b"), "tc-or-phone-like-11-digit"),
    (re.compile(r"\bTR\d{2}(?:[\s]?\d){10,}"), "iban-like"),
    (re.compile(r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email-like"),
    (re.compile(r"\b(?:\d[ -]?){13,}\d\b"), "card-like-14plus-digit"),
]


def redact_pii(text: str) -> tuple[str, int]:
    """Best-effort PII redaction. Return (redacted_text, number_of_redacted_spans)."""
    count = 0
    result = text
    for pattern, replacement in _REDACT_PATTERNS:
        result, n = pattern.subn(replacement, result)
        count += n
    return result, count


def residual_pii_labels(redacted_text: str) -> list[str]:
    """Detector labels that still fire on the REDACTED text (diagnostic, no values)."""
    return [label for det, label in _RESIDUAL_DETECTORS if det.search(redacted_text)]


def assert_no_residual_pii(redacted_text: str) -> None:
    """Fail-closed gate (ADR-0043 D3): raise `RedactionError` if a likely-PII shape
    survived redaction. Call before any real-LLM send. Message is transcript-free."""
    labels = residual_pii_labels(redacted_text)
    if labels:
        raise RedactionError(f"residual PII after redaction: {', '.join(sorted(labels))}")
