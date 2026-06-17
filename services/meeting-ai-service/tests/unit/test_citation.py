"""citation.py tests — grounding + hallucination guard (pure logic, no LLM)."""

from __future__ import annotations

from app.services.citation import (
    ground_claim,
    ground_claims,
    split_sentences,
)

TRANSCRIPT = (
    "Bütçe artışı yönetim kurulunda onaylandı. "
    "Ali raporu cuma gününe kadar hazırlayacak. "
    "Bir sonraki toplantı pazartesi yapılacak."
)


def test_split_sentences_keeps_offsets() -> None:
    sents = split_sentences(TRANSCRIPT)
    assert len(sents) == 3
    assert sents[0].index == 0
    # offsets point back into the transcript
    assert TRANSCRIPT[sents[1].start_char : sents[1].end_char] == sents[1].text
    assert "Ali raporu" in sents[1].text


def test_grounded_claim_cites_source_sentence() -> None:
    # a decision lifted from sentence 0 → grounded, cites it
    c = ground_claim("Bütçe artışı onaylandı", split_sentences(TRANSCRIPT))
    assert c.grounded is True
    assert c.source_index == 0
    assert "Bütçe artışı" in c.source_text
    assert c.similarity >= 0.4


def test_action_grounds_to_its_sentence() -> None:
    c = ground_claim("Ali raporu hazırlayacak", split_sentences(TRANSCRIPT))
    assert c.grounded is True
    assert c.source_index == 1


def test_hallucination_is_flagged_ungrounded() -> None:
    # a claim with no basis in the transcript → ungrounded (the guard)
    c = ground_claim("Şirket yeni bir ofis satın aldı", split_sentences(TRANSCRIPT))
    assert c.grounded is False
    assert c.source_index == -1
    assert c.source_text == ""


def test_ground_claims_counts_ungrounded() -> None:
    claims = [
        "Bütçe artışı onaylandı",  # grounded
        "Toplantı pazartesi yapılacak",  # grounded
        "Genel müdür istifa etti",  # hallucination
    ]
    citations, ungrounded = ground_claims(claims, TRANSCRIPT)
    assert len(citations) == 3
    assert ungrounded == 1
    assert [c.grounded for c in citations] == [True, True, False]


def test_empty_claims_skipped() -> None:
    citations, ungrounded = ground_claims(["", "   "], TRANSCRIPT)
    assert citations == [] and ungrounded == 0


def test_no_segments_means_no_timestamp() -> None:
    # backward-compatible: without STT timing, start_sec stays None
    sents = split_sentences(TRANSCRIPT)
    assert all(s.start_sec is None for s in sents)
    c = ground_claim("Ali raporu hazırlayacak", sents)
    assert c.start_sec is None


def test_segments_attach_timestamp_to_citation() -> None:
    # STT-style timing → each sentence/citation gets its segment start_sec
    segments = [
        {"text": "Bütçe artışı yönetim kurulunda onaylandı.", "start": 0.0, "end": 4.0},
        {"text": "Ali raporu cuma gününe kadar hazırlayacak.", "start": 4.0, "end": 8.5},
        {"text": "Bir sonraki toplantı pazartesi yapılacak.", "start": 8.5, "end": 12.0},
    ]
    c = ground_claim("Ali raporu hazırlayacak", split_sentences(TRANSCRIPT, segments))
    assert c.grounded is True
    assert c.source_index == 1
    assert c.start_sec == 4.0

    citations, _ = ground_claims(
        ["Bütçe artışı onaylandı", "Toplantı pazartesi yapılacak"],
        TRANSCRIPT,
        segments=segments,
    )
    assert [c.start_sec for c in citations] == [0.0, 8.5]


def test_service_response_carries_citations() -> None:
    """End-to-end: MeetingAnalysisService grounds mock decisions/actions."""
    from app.core.config import Settings
    from app.services.analyze import MeetingAnalysisService

    svc = MeetingAnalysisService(Settings(backend="mock", redact_pii=False))
    r = svc.analyze("Bütçe artışı onaylandı. Ali raporu cuma hazırlayacak.")
    # mock picks "onaylandı" → decision, "hazırla" → action; both from transcript
    assert len(r.citations) >= 1
    assert all(c.grounded for c in r.citations)
    assert r.ungrounded_count == 0
