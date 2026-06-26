"""ADR-0043 D4 entailment hardening — the gates token-overlap alone misses.

These pin the differentiator (machine-checked claim↔span entailment) that no
competitor does: polarity contradiction, number mismatch, generic-span, the
hash/offset citation key, and the D8.1 fail-closed withholding of ungrounded
claims from the user-visible output.
"""

from __future__ import annotations

from app.services.citation import (
    CitationStatus,
    ground_claim,
    owner_supported_by_source,
    split_sentences,
)

TRANSCRIPT = (
    "Bütçe artışı yönetim kurulunda onaylandı. "
    "Zam oranı yüzde 12 olarak belirlendi. "
    "Ali raporu cuma gününe kadar hazırlayacak."
)


def _sents():  # type: ignore[no-untyped-def]
    return split_sentences(TRANSCRIPT)


def test_polarity_contradiction_is_rejected() -> None:
    # High token overlap (bütçe, artışı) but OPPOSITE polarity → NOT entailed.
    # Plain overlap would (wrongly) ground this; the polarity gate catches it.
    c = ground_claim("Bütçe artışı reddedildi", _sents())
    assert c.grounded is False
    assert c.status == CitationStatus.FAILED
    assert "polarity" in c.reason


def test_same_polarity_claim_still_grounds() -> None:
    c = ground_claim("Bütçe artışı onaylandı", _sents())
    assert c.grounded is True
    assert c.status == CitationStatus.PASSED


def test_number_mismatch_is_rejected() -> None:
    # "yüzde 20" cited to "yüzde 12" — overlap high, but the quantity is wrong.
    c = ground_claim("Zam oranı yüzde 20 belirlendi", _sents())
    assert c.grounded is False
    assert "number" in c.reason


def test_number_match_grounds() -> None:
    c = ground_claim("Zam oranı yüzde 12 belirlendi", _sents())
    assert c.grounded is True


def test_generic_span_is_low_confidence_not_shipped() -> None:
    sents = split_sentences("Tamam. Bütçe artışı onaylandı.")
    # A claim that best-matches the 1-word "Tamam." span must not ground a decision.
    c = ground_claim("Tamam", sents)
    assert c.grounded is False
    assert c.status in (CitationStatus.LOW_CONFIDENCE, CitationStatus.FAILED)


def test_grounded_citation_carries_hash_offset_key() -> None:
    c = ground_claim("Bütçe artışı onaylandı", _sents())
    assert c.grounded is True
    assert c.source_char_start >= 0
    assert c.source_char_end > c.source_char_start
    assert len(c.source_hash) == 64  # sha256 hex
    assert len(c.quote_hash) == 64
    # offset pins back into the transcript
    assert TRANSCRIPT[c.source_char_start : c.source_char_end] == c.source_text


def test_double_negation_false_pass_is_caught() -> None:
    # Codex 019ee9a6 BLOCKER: "iptal edildi" (cancelled) vs "iptal edilmedi" (NOT
    # cancelled) both carry "iptal" — a flat negation-cue check passes this (the bug).
    # Signed polarity must flag it.
    sents = split_sentences("Proje iptal edilmedi.")
    c = ground_claim("Proje iptal edildi", sents)
    assert c.grounded is False
    assert "polarity" in c.reason


def test_negation_attached_to_outcome_not_whole_sentence() -> None:
    # claim asserts NOT approved; span has an unrelated "yok" + an affirmed approval.
    sents = split_sentences("Bütçe yok, ödeme onaylandı.")
    c = ground_claim("Ödeme onaylanmadı", sents)
    assert c.grounded is False
    assert "polarity" in c.reason


def test_future_negation_outcome_is_caught() -> None:
    sents = split_sentences("Toplantı yapılmayacak.")
    c = ground_claim("Toplantı yapılacak", sents)
    assert c.grounded is False


def test_pending_approval_not_grounded_as_done() -> None:
    # "onaylandı" (done) cited to "onaylanması için bekleniyor" (pending) must NOT pass.
    sents = split_sentences("Sözleşmenin onaylanması için hukuk bekleniyor.")
    c = ground_claim("Sözleşme onaylandı", sents)
    assert c.grounded is False


def test_d8_1_ungrounded_decision_is_withheld() -> None:
    """ADR-0043 D8.1: a hallucinated decision is NOT shipped — moved to rejected_claims."""
    from app.core.config import Settings
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="özet",
                decisions=["Bütçe artışı onaylandı", "Şirket yeni fabrika açtı"],
                action_items=[],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    r = svc.analyze("Bütçe artışı yönetim kurulunda onaylandı.")

    assert "Bütçe artışı onaylandı" in r.decisions  # grounded → shipped
    assert "Şirket yeni fabrika açtı" not in r.decisions  # hallucination → withheld
    assert r.ungrounded_count == 1
    assert any(rc.claim == "Şirket yeni fabrika açtı" for rc in r.rejected_claims)
    # every shipped decision has a PASSED citation
    assert all(c.status == "PASSED" for c in r.citations)


def test_action_owner_must_be_in_same_source_sentence() -> None:
    assert owner_supported_by_source("Ali", "Ali raporu cuma gününe kadar hazırlayacak.")
    assert owner_supported_by_source(
        "Kalite Ekibi", "Kalite Ekibi raporu cuma gününe kadar hazırlayacak."
    )
    assert not owner_supported_by_source("Ali", "Rapor cuma gününe kadar hazırlanacak.")


def test_action_owner_absent_from_grounded_source_is_withheld() -> None:
    """A grounded action can ship, but an unsupported assignee cannot."""
    from app.core.config import Settings
    from app.models.schemas import ActionItem
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="özet",
                decisions=[],
                action_items=[ActionItem(text="Rapor cuma gününe kadar hazırlanacak", owner="Ali")],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze("Rapor cuma gününe kadar hazırlanacak. Ali toplantıya katılmadı.")

    assert len(result.action_items) == 1
    assert result.action_items[0].text == "Rapor cuma gününe kadar hazırlanacak"
    assert result.action_items[0].owner is None
    assert result.ungrounded_count == 1
    assert result.rejected_claims[0].kind == "action_owner"
    assert "owner" in result.rejected_claims[0].reason


def test_action_owner_in_grounded_source_is_kept() -> None:
    from app.core.config import Settings
    from app.models.schemas import ActionItem
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="özet",
                decisions=[],
                action_items=[
                    ActionItem(text="Ali raporu cuma gününe kadar hazırlayacak", owner="Ali")
                ],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze("Ali raporu cuma gününe kadar hazırlayacak.")

    assert result.action_items[0].owner == "Ali"
    assert result.ungrounded_count == 0
