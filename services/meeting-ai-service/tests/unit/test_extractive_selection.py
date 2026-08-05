"""gitops#3444 — extractive-by-construction selection.

The claim this file has to defend is strong: a selected claim is ALWAYS
groundable, not usually. So the tests do not stop at "the parser works" — they
feed selections through the real verifier (`ground_claim`) and assert
`grounded is True`, and they assert that the invalid-selection paths drop
rather than repair.

Measured motivation (k3d-test 2026-08-05): the free-text prompt produced a
median claim coverage of 0.33 against a 0.65 threshold, withholding 29% of
summaries — the model does not obey "copy verbatim" no matter how loudly the
prompt says it.
"""

from __future__ import annotations

from app.services.citation import ground_claim, split_sentences
from app.services.extractive import (
    MAX_SUMMARY_SENTENCES,
    looks_like_selection,
    materialize_action_items,
    materialize_selection,
    number_transcript,
    selectable_sentences,
)

TRANSCRIPT = (
    "Bugün bütçe raporunu konuştuk. "
    "Raporun cuma gününe kadar tamamlanmasına karar verdik. "
    "Bu görevi birinci ekip üstlenecek. "
    "Tamam. "
    "Toplantıyı burada bitiriyoruz."
)


def _menu():
    return selectable_sentences(split_sentences(TRANSCRIPT))


class TestStructuralGroundingGuarantee:
    """The whole point: selection cannot produce an ungroundable claim."""

    def test_every_selectable_sentence_grounds_when_selected(self) -> None:
        menu = _menu()
        sentences = split_sentences(TRANSCRIPT)
        for index in range(1, len(menu) + 1):
            (claim,) = materialize_selection([index], menu, MAX_SUMMARY_SENTENCES)
            citation = ground_claim(claim, sentences)
            assert citation.grounded, f"index {index} ({claim!r}) grounded={citation.grounded}"
            assert citation.similarity == 1.0

    def test_multi_sentence_summary_grounds_sentence_by_sentence(self) -> None:
        menu = _menu()
        sentences = split_sentences(TRANSCRIPT)
        claims = materialize_selection([1, 2], menu, MAX_SUMMARY_SENTENCES)
        assert len(claims) == 2
        for claim in claims:
            assert ground_claim(claim, sentences).grounded


class TestSelectionIsValidatedNeverRepaired:
    def test_out_of_range_indices_are_dropped(self) -> None:
        menu = _menu()
        # 0 and 99 do not name a sentence; 2 does. Nothing may be clamped into
        # a neighbouring sentence — that would fabricate attribution.
        claims = materialize_selection([0, 99, 2], menu, MAX_SUMMARY_SENTENCES)
        assert claims == [menu[1].text]

    def test_duplicate_indices_collapse(self) -> None:
        menu = _menu()
        assert materialize_selection([2, 2, 2], menu, MAX_SUMMARY_SENTENCES) == [menu[1].text]

    def test_booleans_and_non_integers_are_not_indices(self) -> None:
        menu = _menu()
        # JSON `true` must not become index 1, and "2" is not an index.
        assert materialize_selection([True, "2", None, 1.5], menu, MAX_SUMMARY_SENTENCES) == []

    def test_budget_caps_the_selection(self) -> None:
        menu = _menu()
        claims = materialize_selection(list(range(1, len(menu) + 1)), menu, 2)
        assert len(claims) == 2

    def test_non_list_selection_yields_nothing(self) -> None:
        assert materialize_selection("1,2", _menu(), MAX_SUMMARY_SENTENCES) == []

    def test_empty_selection_is_a_legitimate_answer(self) -> None:
        assert materialize_selection([], _menu(), MAX_SUMMARY_SENTENCES) == []


class TestActionItems:
    def test_action_text_is_the_exact_sentence_and_grounds(self) -> None:
        menu = _menu()
        sentences = split_sentences(TRANSCRIPT)
        items = materialize_action_items(
            [{"sentence": 3, "owner": "birinci ekip", "due_date": "cuma"}], menu
        )
        assert len(items) == 1
        text, owner, due = items[0]
        assert text == menu[2].text
        assert ground_claim(text, sentences).grounded
        assert owner == "birinci ekip"
        assert due == "cuma"

    def test_item_with_invalid_sentence_is_dropped_whole(self) -> None:
        # A dangling owner with no action text is worse than no item.
        assert materialize_action_items([{"sentence": 99, "owner": "Ali"}], _menu()) == []

    def test_blank_owner_and_due_date_become_null(self) -> None:
        items = materialize_action_items([{"sentence": 2, "owner": "   ", "due_date": ""}], _menu())
        assert items[0][1] is None
        assert items[0][2] is None

    def test_non_dict_items_are_skipped(self) -> None:
        assert materialize_action_items(["cuma", 3, None], _menu()) == []


class TestMenuConstruction:
    def test_filler_sentences_are_not_offered(self) -> None:
        # "Tamam." would be rejected downstream as too generic evidence; it must
        # not consume a slot in the model's menu.
        assert all(s.text.strip() != "Tamam." for s in _menu())

    def test_numbering_is_one_based_and_quotes_sentences_verbatim(self) -> None:
        menu = _menu()
        rendered = number_transcript(menu)
        first = rendered.splitlines()[0]
        assert first.startswith("[1] ")
        assert first[4:] == menu[0].text

    def test_menu_indices_address_the_verifier_s_own_sentences(self) -> None:
        # The alignment invariant: the menu is built from split_sentences, so a
        # materialized claim is byte-identical to a sentence the verifier finds.
        menu = _menu()
        verifier_texts = {s.text for s in split_sentences(TRANSCRIPT)}
        for sentence in menu:
            assert sentence.text in verifier_texts


class TestContractDetection:
    def test_selection_shape_is_recognised(self) -> None:
        assert looks_like_selection({"summary_sentences": [1]})
        assert looks_like_selection({"decision_sentences": []})

    def test_legacy_free_text_shape_is_not_mistaken_for_selection(self) -> None:
        # A model that ignores the new prompt must fall back, not crash.
        assert not looks_like_selection({"summary": "…", "decisions": [], "action_items": []})
        assert not looks_like_selection("nope")


class TestGuaranteeHoldsOnRealTranscriptShapes:
    """Two defects found by running the guarantee against 23 production
    transcripts (776 selectable sentences). Both are pinned here because each
    silently downgraded the guarantee to a tendency.
    """

    def test_single_content_token_spans_are_not_offered(self) -> None:
        # 15/762 menu entries were spans like "Yunanistan için." — one content
        # token, so `ground_claim` returns LOW_CONFIDENCE even quoted verbatim.
        # The menu filter must use the verifier's own bar, not a char count.
        transcript = "Yunanistan için. Bugün bütçe raporunu ayrıntılı biçimde konuştuk."
        menu = selectable_sentences(split_sentences(transcript))
        assert [s.text for s in menu] == ["Bugün bütçe raporunu ayrıntılı biçimde konuştuk."]

    def test_duplicate_lookalike_sentence_cites_the_exact_one(self) -> None:
        # Real transcript: the same line appears with and without a bare digit.
        # Coverage cannot separate them (a digit is not a content token), so
        # first-wins cited the look-alike and then failed the number gate —
        # rejecting a claim that is verbatim present.
        transcript = (
            "Bugün ofiste oldukça yoğun bir gün geçirdik. "
            "Bugün ofiste oldukça yoğun bir 1 gün geçirdik."
        )
        sentences = split_sentences(transcript)
        menu = selectable_sentences(sentences)
        (claim,) = materialize_selection([2], menu, MAX_SUMMARY_SENTENCES)
        citation = ground_claim(claim, sentences)
        assert citation.grounded
        assert citation.source_text == claim

    def test_every_offered_sentence_grounds_for_a_realistic_transcript(self) -> None:
        transcript = (
            "Bugün bütçe raporunu konuştuk. Tamam. "
            "Raporun cuma gününe kadar tamamlanmasına karar verdik. Evet. "
            "Bu görevi birinci ekip üstlenecek. Bir de bu beş."
        )
        sentences = split_sentences(transcript)
        menu = selectable_sentences(sentences)
        assert menu, "menu must not be empty for a realistic transcript"
        for index in range(1, len(menu) + 1):
            (claim,) = materialize_selection([index], menu, 99)
            assert ground_claim(claim, sentences).grounded, claim
