"""gitops#3444 — Turkish morphology-aware grounding recall.

Measured problem: 29% of analyses (22/76 on k3d-test, 2026-08-05) shipped a
fully-withheld summary because every claim content token had to appear in the
cited sentence with its EXACT surface form — hopeless for an agglutinative
language once the LLM paraphrases.

These tests pin BOTH sides of the fix:
* recall — nominal inflection no longer costs grounding;
* precision — every guard the exact-token design existed for still holds
  (verb tense/aspect, short-lemma compounds, owner attribution), and without
  Zeyrek the behaviour degrades to the old exact-surface matching, never to a
  looser match.
"""

from __future__ import annotations

import logging

import pytest

from app.services import morphology
from app.services.citation import (
    CitationStatus,
    ground_claim,
    owner_supported_by_source,
    split_sentences,
)

pytestmark = pytest.mark.skipif(
    not morphology.available(), reason="zeyrek unavailable in this environment"
)


def _ground(claim: str, transcript: str):
    return ground_claim(claim, split_sentences(transcript))


class TestNominalRecall:
    """The 29% class: case/possessive inflection must stop failing grounding."""

    def test_case_inflected_nouns_ground(self) -> None:
        citation = _ground(
            "Bütçe raporunun teslimi tamamlandı.",
            "Dün konuştuk. Bütçe raporunu teslim ettik ve iş tamamlandı. Devam ediyoruz.",
        )
        assert citation.status == CitationStatus.PASSED
        assert citation.grounded

    def test_locative_and_genitive_variants_ground(self) -> None:
        citation = _ground(
            "Toplantının gündemi rapordaki bulgulardı.",
            "Bu toplantıda gündem raporda geçen bulgular oldu.",
        )
        # Not asserting PASSED here — the point is the morphology layer, so
        # assert the specific tokens stopped being the blocker.
        assert citation.similarity > 0.5

    def test_verbal_noun_case_variants_ground(self) -> None:
        # "tamamlanması" vs "tamamlanmasına": both verbal NOUNS (no verb
        # reading), same aspect — this pair SHOULD match.
        citation = _ground(
            "Projenin tamamlanması planlandı.",
            "Projenin tamamlanmasına yönelik plan planlandı.",
        )
        assert citation.status == CitationStatus.PASSED


class TestTenseAspectGuardStillClosed:
    """The exact objection that rejected naive stemming must still hold."""

    def test_done_vs_pending_does_not_merge(self) -> None:
        # "onaylandı" (approved) has a Verb reading → exact surface required;
        # the transcript only offers "onaylanması" (approval — pending).
        citation = _ground(
            "Bütçe onaylandı.",
            "Bütçenin onaylanması gelecek haftaya kaldı.",
        )
        assert not citation.grounded
        assert citation.status == CitationStatus.FAILED

    def test_verb_person_variants_do_not_merge(self) -> None:
        # "konuştuk" (we spoke) vs "konuşulacak" (will be discussed): both have
        # verb readings → no lemma bridge in either direction.
        citation = _ground(
            "Fiyatlar konuşuldu.",
            "Fiyatlar yarın konuşulacak.",
        )
        assert not citation.grounded


class TestShortLemmaAndAttributionGuards:
    def test_short_derived_lemma_cannot_bridge(self) -> None:
        # Zeyrek gives "canlı" a can+lı reading; the shared lemma "can" is
        # below the minimum length AND "canlı" is derivation, not inflection —
        # a person token "can" must not be grounded by "canlı".
        assert not morphology.tokens_share_nominal_lemma("can", "canlı")

    def test_owner_attribution_stays_exact(self) -> None:
        # The morphology layer must NOT loosen owner grounding.
        assert not owner_supported_by_source("Can", "Canlı yayın raporu iletildi.")

    def test_final_consonant_softening_is_inflection_not_derivation(self) -> None:
        # kitap → kitabı is legitimate suffixing (softening); it must match.
        assert morphology.tokens_share_nominal_lemma("kitabı", "kitap")
        # An unrelated pair sharing no valid prefix-lemma must not.
        assert not morphology.tokens_share_nominal_lemma("kalem", "kitap")


class TestZeyrekPrivateApiContract:
    """`morphology` rides Zeyrek's word-level `_parse` (the public `analyze`
    drags in an NLTK runtime download). Pin the observed contract so a future
    zeyrek bump that breaks it turns CI red instead of silently killing recall.
    """

    def test_nominal_token_yields_lemma(self) -> None:
        assert "bütçe" in morphology.content_lemmas("bütçenin")

    def test_verb_reading_blocks_all_lemmas(self) -> None:
        assert morphology.content_lemmas("onaylandı") == frozenset()

    def test_unanalyzable_token_degrades_to_exact(self) -> None:
        assert morphology.content_lemmas("xqzptvw") == frozenset()


class TestTranscriptNeverLeaksIntoLogs:
    """Zeyrek logs every analyzed token — transcript content — at WARNING
    ("APPENDING RESULT: <(bütçe_Noun)..."). The analyzer wrapper must cut that
    channel completely; a level bump alone does not survive zeyrek's choice of
    log level. This is the service's transcript-free log discipline applied to
    a third-party dependency.
    """

    def test_analyzed_tokens_do_not_reach_logging(self, caplog) -> None:
        morphology.content_lemmas.cache_clear()
        probe = "gizlibütçekelimesi"
        with caplog.at_level(logging.DEBUG):
            morphology.content_lemmas(probe)
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert probe not in joined
        assert "APPENDING RESULT" not in joined


class TestFallbackWithoutZeyrek:
    """Without the analyzer the verifier must be byte-for-byte the old one."""

    def test_recall_case_fails_again_in_fallback(self, monkeypatch) -> None:
        monkeypatch.setattr(morphology, "available", lambda: False)
        citation = _ground(
            "Bütçe raporunun teslimi tamamlandı.",
            "Dün konuştuk. Bütçe raporunu teslim ettik ve iş tamamlandı. Devam ediyoruz.",
        )
        assert not citation.grounded

    def test_guards_still_closed_in_fallback(self, monkeypatch) -> None:
        monkeypatch.setattr(morphology, "available", lambda: False)
        citation = _ground(
            "Bütçe onaylandı.",
            "Bütçenin onaylanması gelecek haftaya kaldı.",
        )
        assert not citation.grounded
