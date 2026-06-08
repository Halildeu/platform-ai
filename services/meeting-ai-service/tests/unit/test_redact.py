"""PII redaction unit tests — the KVKK boundary before any LLM call."""

from __future__ import annotations

from app.services.redact import redact_pii


def test_email_redacted() -> None:
    out, n = redact_pii("İletişim: ali@example.com lütfen")
    assert "ali@example.com" not in out
    assert "***REDACTED_EMAIL***" in out
    assert n == 1


def test_tc_kimlik_redacted() -> None:
    out, n = redact_pii("TC 12345678901 kayıt edildi")
    assert "12345678901" not in out
    assert "***REDACTED_TC***" in out
    assert n == 1


def test_iban_redacted() -> None:
    out, n = redact_pii("IBAN TR330006100519786993745634 hesabı")
    assert "TR330006100519786993745634" not in out
    assert "***REDACTED_IBAN***" in out
    assert n == 1


def test_phone_redacted() -> None:
    out, n = redact_pii("Numaram 0532 123 45 67")
    assert "***REDACTED_PHONE***" in out
    assert n == 1


def test_bearer_redacted() -> None:
    out, n = redact_pii("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert n >= 1


def test_clean_text_untouched() -> None:
    text = "Toplantıda bütçe konuşuldu ve karar verildi."
    out, n = redact_pii(text)
    assert out == text
    assert n == 0
