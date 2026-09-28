"""Reproduce limitations, NOT a passing task-quality gate. No network/model calls."""

import hashlib

from app.models.schemas import AnalyzeResponse, Citation
from app.services.citation import (
    due_date_supported_by_source,
    ground_claim,
    owner_supported_by_source,
    split_sentences,
)
from app.services.extractive import (
    MAX_ACTION_ITEMS,
    materialize_action_items,
    selectable_sentences,
)
from app.services.live_context import live_menu, result_cursor


def test_a_grounded_quote_does_not_prove_a_complete_action_or_decision() -> None:
    source = split_sentences("11 olacak.")
    assert selectable_sentences(source) == source
    assert ground_claim("11 olacak.", source).grounded
    assert materialize_action_items(
        [{"sentence": 1, "owner": None, "due_date": "11 olacak"}], source
    ) == [("11 olacak.", None, "11 olacak")]
    # Quotation/materialization still succeeds, but the final analysis-specific
    # completeness guard now withholds it. See test_analysis_fragment_guard.py.
    # This alone does not attach the new time to its existing task.
    assert not owner_supported_by_source("Zeynep", source[0].text)


def test_correct_updated_task_cannot_be_expressed_by_one_selected_sentence() -> None:
    old = "Zeynep sunum dosyasını 28 Eylül 2026 günü saat 10'a kadar tamamlayacak."
    source = split_sentences(old + " Zeynep'in sunum dosyasını teslim saati 10 değil. 11 olacak.")
    expected = old.replace("10'a", "11'e")
    assert len(source) == 3
    assert expected not in {s.text for s in source}
    verdict = ground_claim(expected, source)
    assert not verdict.grounded
    assert "number/quantity" in verdict.reason
    assert not due_date_supported_by_source("28 Eylül 2026 günü saat 11'e kadar", old)
    assert not owner_supported_by_source("Zeynep", source[-1].text)


def test_once_omitted_task_can_disappear_from_the_next_live_menu() -> None:
    task = "Zeynep sunum dosyasını hazırlayacak."
    other = "Mehmet bütçe tablosunu kontrol edecek."
    background = " ".join(f"Ekip {n} numaralı gündem maddesini görüştü." for n in range(8))
    old = f"{task} {other} {background}"
    result = AnalyzeResponse(
        summary="",
        citations=[Citation(claim=other, source_index=1, similarity=1, grounded=True)],
        redacted=True,
        redaction_count=0,
        backend="mock",
        model="NONE",
        elapsed_ms=0,
    )
    hint = result_cursor(old, result)
    assert hint.source_sha256 == hashlib.sha256(old.encode()).hexdigest()
    extended = old + " Katılımcılar gelecek haftanın gündemini görüştü."
    source = split_sentences(extended)
    assert task in {s.text for s in source}
    assert task not in {s.text for s in live_menu(extended, source, hint)}
    assert other in {s.text for s in live_menu(extended, source, hint)}
    # There is no cancellation: exclusion comes solely from the last model selection.


def test_task_count_ceiling_can_hide_valid_work_even_with_perfect_selection() -> None:
    source = split_sentences(
        " ".join(f"Ekip {n} numaralı raporu hazırlayacak." for n in range(MAX_ACTION_ITEMS + 1))
    )
    items = [{"sentence": i + 1, "owner": None, "due_date": None} for i in range(len(source))]
    assert len(source) == MAX_ACTION_ITEMS + 1
    assert len(materialize_action_items(items, source)) == MAX_ACTION_ITEMS
