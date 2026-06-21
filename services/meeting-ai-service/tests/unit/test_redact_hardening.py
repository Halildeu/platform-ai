"""ADR-0043 D3 redaction hardening — expanded patterns + fail-closed residual gate."""

from __future__ import annotations

import pytest

from app.services.redact import (
    RedactionError,
    assert_no_residual_pii,
    redact_pii,
    residual_pii_labels,
)


def test_plate_is_redacted() -> None:
    out, n = redact_pii("Araç 34 ABC 1234 plakası girişte görüldü.")
    assert n >= 1
    assert "34 ABC 1234" not in out
    assert "REDACTED_PLATE" in out


def test_card_is_redacted() -> None:
    out, n = redact_pii("Ödeme 1234 5678 9012 3456 kartından yapıldı.")
    assert "1234 5678 9012 3456" not in out
    assert "REDACTED_CARD" in out


def test_passport_is_redacted() -> None:
    out, _ = redact_pii("Pasaport U12345678 ibraz edildi.")
    assert "U12345678" not in out
    assert "REDACTED_PASSPORT" in out


def test_existing_tc_email_phone_still_redacted() -> None:
    out, n = redact_pii("Ali ali@x.com 12345678901 numarası ve 0532 123 45 67 telefonu.")
    assert n >= 3
    assert "ali@x.com" not in out
    assert "12345678901" not in out


def test_residual_gate_passes_on_clean_redacted_text() -> None:
    redacted, _ = redact_pii("Ali ali@x.com 12345678901 ile iletişime geçti.")
    # Precise patterns caught everything → no residual → no raise.
    assert residual_pii_labels(redacted) == []
    assert_no_residual_pii(redacted)  # must not raise


def test_residual_gate_fails_closed_on_slipped_pii() -> None:
    # An 11-digit starting with 0 is NOT caught by the precise TC pattern ([1-9]\d{10})
    # nor the phone pattern (needs 5xx) → it SURVIVES redaction → the broad residual
    # detector must catch it and fail-closed (KVKK: never send to an LLM).
    redacted, _ = redact_pii("Kayıt numarası 01234567890 olarak girildi.")
    assert "01234567890" in redacted  # precise patterns missed it
    assert residual_pii_labels(redacted)  # broad detector flags it
    with pytest.raises(RedactionError):
        assert_no_residual_pii(redacted)


def test_redaction_error_message_is_transcript_free() -> None:
    redacted, _ = redact_pii("Numara 01234567890 burada.")
    try:
        assert_no_residual_pii(redacted)
    except RedactionError as exc:
        # only detector labels, never the raw value
        assert "01234567890" not in str(exc)
        assert "11-digit" in str(exc)
    else:
        pytest.fail("expected RedactionError")
