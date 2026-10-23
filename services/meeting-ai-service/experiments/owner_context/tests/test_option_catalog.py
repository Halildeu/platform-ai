from dataclasses import replace

import pytest

from experiments.owner_context.candidate import Snapshot
from experiments.owner_context.local_compare import artificial
from experiments.owner_context.option_catalog import (
    CatalogAction,
    CatalogSelection,
    evaluate,
    prepare_options,
    prompt,
    selection_schema,
)


def select(
    task: int = 1, option: str | None = None, owner: str | None = None, due: str | None = None
) -> CatalogSelection:
    return CatalogSelection(
        action_items=[
            CatalogAction(task_source=task, owner_option=option, owner=owner, due_date=due)
        ]
    )


def separated(name: str = "Mehmet.", task: str = "Bütçe tablosunu kontrol edecek.") -> Snapshot:
    return artificial([(name, "S1"), (task, "S1")])


def test_link_materializes_original_evidence_without_rewriting_punctuation() -> None:
    snapshot = separated()
    (link,) = prepare_options(snapshot)
    output = evaluate(snapshot, select(option=link.option_id))
    assert output["actions"] == [
        {"text": snapshot.sources[1].text, "owner": "Mehmet", "due_date": None}
    ]
    assert output["evidence"][0]["task"]["source_index"] == 1
    assert [s["source_index"] for s in output["evidence"][0]["ownerEvidence"]] == [0, 1]
    assert snapshot.text.startswith("Mehmet.")
    assert output["punctuationChanged"] is False
    assert output["semanticQualityVerified"] is False


@pytest.mark.parametrize("name", ["Mehmet?", "Mehmet!", "Mehmet...", "2026."])
def test_non_candidate_punctuation_and_numbers(name: str) -> None:
    assert prepare_options(separated(name)) == ()


@pytest.mark.parametrize("name", ["Hayır.", "Tamam.", "Not."])
def test_non_names_are_candidates_not_auto_assignments(name: str) -> None:
    snapshot = separated(name)
    assert prepare_options(snapshot)
    assert evaluate(snapshot, select())["actions"][0]["owner"] is None
    # A wrong semantic selection can still be wrong: structural tests are not accuracy.
    output = evaluate(snapshot, select(option=prepare_options(snapshot)[0].option_id))
    assert output["semanticQualityVerified"] is False


def test_full_context_keeps_short_fragments_and_retractions() -> None:
    snapshot = artificial(
        [
            ("Mehmet.", "S1"),
            ("Raporu hazırlayacak.", "S1"),
            ("Bu görev iptal edildi; rapor hazırlanmayacak.", "S1"),
        ]
    )
    value = prompt(snapshot)
    assert "Mehmet." in value and "iptal edildi" in value


def test_link_is_pinned_to_snapshot_and_task() -> None:
    snapshot = separated()
    (link,) = prepare_options(snapshot)
    changed = artificial(
        [("Mehmet.", "S1"), ("Bütçe tablosunu kontrol edecek.", "S1"), ("Son bilgi eklendi.", "S1")]
    )
    result = evaluate(changed, select(option=link.option_id))
    assert result["actions"][0]["owner"] is None
    assert result["rejected"] == ["unknown-stale-or-wrong-task-link"]
    two_tasks = artificial(
        [
            ("Mehmet.", "S1"),
            ("Raporu hazırlayacak.", "S1"),
            ("Zeynep.", "S1"),
            ("Bütçeyi kontrol edecek.", "S1"),
        ]
    )
    link = prepare_options(two_tasks)[0]
    assert evaluate(two_tasks, select(task=3, option=link.option_id))["actions"][0]["owner"] is None


@pytest.mark.parametrize("owner", ["Ayşe", "Mehmet"])
def test_existing_same_source_owner_preserved(owner: str) -> None:
    snapshot = separated(task=f"Raporu {owner} hazırlayacak.")
    (link,) = prepare_options(snapshot)
    assert (
        evaluate(snapshot, select(owner=owner, option=link.option_id))["actions"][0]["owner"]
        == owner
    )


def test_unsupported_free_text_does_not_fall_back_to_link() -> None:
    snapshot = separated()
    (link,) = prepare_options(snapshot)
    output = evaluate(snapshot, select(owner="Ayşe", option=link.option_id))
    assert output["actions"][0]["owner"] is None
    assert "conflicting-owner-fields" in output["rejected"]


def test_missing_or_different_speaker_does_not_offer_link() -> None:
    snapshot = separated()
    assert prepare_options(replace(snapshot, spans=())) == ()
    assert prepare_options(artificial([("Mehmet.", "S1"), ("Raporu hazırlayacak.", "S2")])) == ()


def test_long_pause_is_not_a_measured_stt_threshold() -> None:
    snapshot = separated()
    late = replace(snapshot.spans[1], start_ms=4500, end_ms=5500)
    snapshot = replace(snapshot, spans=(snapshot.spans[0], late))
    assert prepare_options(snapshot)
    assert prepare_options(snapshot, max_gap_ms=2000) == ()


def test_wrong_or_stale_source_has_no_links() -> None:
    snapshot = separated()
    assert prepare_options(replace(snapshot, sha256="0" * 64)) == ()
    bad = replace(snapshot.sources[0], text="Ayşe.")
    assert prepare_options(replace(snapshot, sources=(bad, snapshot.sources[1]))) == ()


def test_schema_only_allows_actual_catalog_ids() -> None:
    snapshot = separated()
    values = selection_schema(snapshot)["$defs"]["CatalogAction"]["properties"]["owner_option"][
        "enum"
    ]
    assert values == [None, prepare_options(snapshot)[0].option_id]


def test_duplicate_and_invalid_tasks_do_not_materialize() -> None:
    snapshot = separated()
    output = evaluate(
        snapshot,
        CatalogSelection(
            action_items=[
                *select().action_items,
                *select().action_items,
                *select(task=99).action_items,
            ]
        ),
    )
    assert len(output["actions"]) == 1
    assert len(output["rejected"]) == 2


def test_due_date_not_inferred() -> None:
    snapshot = separated()
    output = evaluate(snapshot, select(due="26 Eylül 2026"))
    assert output["actions"][0]["due_date"] is None
    assert output["rejected"] == ["unsupported-date"]


def test_invalid_snapshot_cannot_materialize_even_without_owner_options() -> None:
    snapshot = separated(task="Raporu Ayşe hazırlayacak.")
    altered = replace(snapshot.sources[1], text="Sözleşmeyi Ayşe imzalayacak.")
    for bad in [
        replace(snapshot, sha256="0" * 64),
        replace(snapshot, sources=(snapshot.sources[0], altered)),
    ]:
        output = evaluate(bad, select(owner="Ayşe"))
        assert output["actions"] == []
        assert output["evidence"] == []
        assert output["rejected"] == ["invalid-snapshot"]


def test_known_semantic_limit_omitted_explicit_owner_can_still_be_wrong() -> None:
    snapshot = separated(task="Raporu Ayşe hazırlayacak.")
    (link,) = prepare_options(snapshot)
    output = evaluate(snapshot, select(option=link.option_id))
    # Deliberately retained counterexample: this is NOT an accepted product behavior.
    assert output["actions"][0]["owner"] == "Mehmet"
    assert output["semanticQualityVerified"] is False


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), -1.0])
def test_invalid_clock_cannot_authorize_a_link(bad_time: float) -> None:
    snapshot = separated()
    bad_span = replace(snapshot.spans[0], start_ms=bad_time)
    assert not prepare_options(replace(snapshot, spans=(bad_span, *snapshot.spans[1:])))
