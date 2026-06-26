"""ADR-0043 D4 entailment hardening — the gates token-overlap alone misses.

These pin the differentiator (machine-checked claim↔span entailment) that no
competitor does: polarity contradiction, number mismatch, generic-span, the
hash/offset citation key, and the D8.1 fail-closed withholding of ungrounded
claims from the user-visible output.
"""

from __future__ import annotations

from app.services.citation import (
    CitationStatus,
    due_date_supported_by_source,
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


def test_fused_claim_with_unsupported_clause_is_rejected() -> None:
    # The first clause is well supported by one sentence, but the claim also adds
    # a separate factory claim absent from that cited sentence. A single citation
    # cannot ground fused multi-source prose.
    sents = split_sentences(
        "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi netleşti. "
        "Ali raporu cuma gününe kadar hazırlayacak."
    )
    c = ground_claim(
        "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi netleşti, "
        "şirket yeni fabrika açtı",
        sents,
    )
    assert c.grounded is False
    assert "unsupported content" in c.reason


def test_short_unsupported_clause_is_rejected_even_when_coverage_is_high() -> None:
    # Regression: with a two-token unsupported allowance, the supported long
    # clause below dominated overlap and "fabrika açtı" still shipped. In a
    # regulated product, two unsupported content tokens are enough to change the
    # business fact.
    sents = split_sentences(
        "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi netleşti."
    )
    c = ground_claim(
        "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi "
        "netleşti, fabrika açtı",
        sents,
    )

    assert c.grounded is False
    assert "unsupported content" in c.reason


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
                summary="",
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


def test_action_due_date_must_be_in_same_source_sentence() -> None:
    assert due_date_supported_by_source("cuma", "Ali raporu cuma gününe kadar hazırlayacak.")
    assert due_date_supported_by_source("26.06.2026", "Rapor 26.06.2026 tarihinde teslim edilecek.")
    assert not due_date_supported_by_source(
        "2026-06-26", "Rapor 26.06.2026 tarihinde teslim edilecek."
    )
    assert not due_date_supported_by_source(
        "26 Haziran 2026", "Rapor 26.06.2026 tarihinde teslim edilecek."
    )
    assert not due_date_supported_by_source(
        "2026-06-26", "Ali raporu cuma gününe kadar hazırlayacak."
    )
    assert not due_date_supported_by_source("salı", "Ali raporu cuma gününe kadar hazırlayacak.")


def test_action_owner_absent_from_grounded_source_is_withheld() -> None:
    """A grounded action can ship, but an unsupported assignee cannot."""
    from app.core.config import Settings
    from app.models.schemas import ActionItem
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="",
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


def test_action_due_date_absent_from_grounded_source_is_withheld() -> None:
    """A grounded action can ship, but an unsupported due date cannot."""
    from app.core.config import Settings
    from app.models.schemas import ActionItem
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="",
                decisions=[],
                action_items=[
                    ActionItem(
                        text="Rapor cuma gününe kadar hazırlanacak",
                        due_date="2026-06-26",
                    )
                ],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze("Rapor cuma gününe kadar hazırlanacak.")

    assert result.schema_version == "5-adr0043"
    assert len(result.action_items) == 1
    assert result.action_items[0].text == "Rapor cuma gününe kadar hazırlanacak"
    assert result.action_items[0].due_date is None
    assert result.ungrounded_count == 1
    assert result.rejected_claims[0].kind == "action_due_date"
    assert "due date" in result.rejected_claims[0].reason


def test_action_owner_in_grounded_source_is_kept() -> None:
    from app.core.config import Settings
    from app.models.schemas import ActionItem
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="",
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


def test_action_due_date_in_grounded_source_is_kept() -> None:
    from app.core.config import Settings
    from app.models.schemas import ActionItem
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="",
                decisions=[],
                action_items=[
                    ActionItem(
                        text="Ali raporu cuma gününe kadar hazırlayacak",
                        owner="Ali",
                        due_date="cuma",
                    )
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
    assert result.action_items[0].due_date == "cuma"
    assert result.ungrounded_count == 0


def test_fused_decision_is_withheld_from_analyze_response() -> None:
    """A decision that fuses an unsupported clause into grounded prose is withheld."""
    from app.core.config import Settings
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="",
                decisions=[
                    "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi "
                    "netleşti, şirket yeni fabrika açtı"
                ],
                action_items=[],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze(
        "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi netleşti."
    )

    assert result.schema_version == "5-adr0043"
    assert result.decisions == []
    assert result.ungrounded_count == 1
    assert result.rejected_claims[0].kind == "decision"
    assert "unsupported content" in result.rejected_claims[0].reason


def test_short_fused_decision_is_withheld_from_analyze_response() -> None:
    """A short unsupported fact must not ride along with a long grounded decision."""
    from app.core.config import Settings
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="",
                decisions=[
                    "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi "
                    "netleşti, fabrika açtı"
                ],
                action_items=[],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze(
        "Bütçe artışı yönetim kurulunda oy birliğiyle onaylandı ve ödeme takvimi netleşti."
    )

    assert result.decisions == []
    assert result.ungrounded_count == 1
    assert result.rejected_claims[0].kind == "decision"
    assert "unsupported content" in result.rejected_claims[0].reason


def test_unsupported_summary_sentence_is_withheld() -> None:
    """User-visible summary prose must be filtered like decisions/actions."""
    from app.core.config import Settings
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="Bütçe artışı onaylandı. Şirket yeni fabrika açtı.",
                decisions=[],
                action_items=[],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze("Bütçe artışı onaylandı.")

    assert result.summary == "Bütçe artışı onaylandı."
    assert result.summary_grounding_status == "partial_verified"
    assert len(result.summary_citations) == 1
    assert result.summary_citations[0].claim == "Bütçe artışı onaylandı."
    assert result.ungrounded_count == 0
    assert len(result.rejected_claims) == 1
    assert result.rejected_claims[0].kind == "summary"
    assert result.rejected_claims[0].claim == "Şirket yeni fabrika açtı."
    assert "fabrika" not in result.summary


def test_fully_ungrounded_summary_uses_fixed_safe_text() -> None:
    from app.core.config import Settings
    from app.services.analyze import AnalysisDraft, MeetingAnalysisService

    class _StubAnalyzer:
        def analyze(self, transcript: str) -> AnalysisDraft:
            return AnalysisDraft(
                summary="Şirket yeni fabrika açtı.",
                decisions=[],
                action_items=[],
            )

        @property
        def model_loaded(self) -> bool:
            return True

    svc = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=_StubAnalyzer()
    )
    result = svc.analyze("Bütçe artışı onaylandı.")

    assert result.summary == ""
    assert result.summary_grounding_status == "withheld"
    assert result.summary_citations == []
    assert result.ungrounded_count == 0
    assert len(result.rejected_claims) == 1
    assert result.rejected_claims[0].kind == "summary"
