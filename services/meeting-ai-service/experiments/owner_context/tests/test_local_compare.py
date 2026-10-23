"""Evaluation plumbing checks; no real model is called by these tests."""

from experiments.owner_context.local_compare import (
    ContextAction,
    ContextSelection,
    artificial,
    cases,
    evaluate_candidate,
    grade,
    proposal_prompt,
)


def test_grader_rejects_right_count_with_wrong_owner_or_year() -> None:
    expected = [{"text": "Sunumu hazırlayacak.", "owner": "Zeynep", "due_date": "2026"}]
    assert grade(expected, expected)["exact"] is True
    assert grade([expected[0] | {"owner": "Mehmet"}], expected)["wrongNamedOwners"] == 1
    assert grade([expected[0] | {"due_date": "2016"}], expected)["exact"] is False
    assert grade([expected[0], expected[0]], expected)["exact"] is False


def test_matrix_covers_both_recorded_inputs_and_unsafe_attribution_cases() -> None:
    matrix = {row["id"]: row for row in cases()}
    assert {
        "normal",
        "paused",
        "vocative-first-person",
        "different-speaker",
        "cancelled-task",
        "reassigned-task",
    } <= matrix.keys()
    assert matrix["normal"]["snapshot"].sha256 == (
        "5a889d11d1ab70acec1ddd8289464f795a7e9ae011091b1df4a776711ca8d7e3"
    )
    assert [a["owner"] for a in matrix["normal"]["expected"]] == ["Zeynep", "Mehmet"]
    assert matrix["cancelled-task"]["expected"] == []


def test_prompt_contains_only_source_data_and_policy_not_grading_annotations() -> None:
    row = next(row for row in cases() if row["id"] == "normal")
    rendered = proposal_prompt(row["snapshot"])
    assert "Mehmet." in rendered
    assert '"source_index": 3' in rendered
    assert '"expected"' not in rendered
    assert '"observedActions"' not in rendered
    assert '"grade"' not in rendered


def test_no_owner_keeps_first_person_task_and_invalid_dates_are_not_invented() -> None:
    snapshot = artificial([("Bütçeyi ben kontrol edeceğim.", "S1")])
    selection = ContextSelection(
        action_items=[
            ContextAction(
                task_source=0,
                owner_source=None,
                owner=None,
                due_date="yarın",
                relationship="ambiguous",
            )
        ]
    )
    result = evaluate_candidate(snapshot, selection)
    assert result["actions"] == [{"text": snapshot.text, "owner": None, "due_date": None}]
    assert result["rejected"] == ["unsupported-date"]
    assert result["evidence"][0]["task"]["text"] == snapshot.text


def test_invalid_duplicate_or_retracted_actions_do_not_become_valid_output() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Bütçeyi kontrol edecek.", "S1")])
    item = ContextAction(
        task_source=1,
        owner_source=0,
        owner="Mehmet",
        due_date=None,
        relationship="subject_continuation",
    )
    selection = ContextSelection(
        action_items=[
            item,
            item,
            item.model_copy(update={"task_source": 99}),
            item.model_copy(update={"task_source": 0, "relationship": "retracted"}),
        ]
    )
    result = evaluate_candidate(snapshot, selection)
    assert len(result["actions"]) == 1
    assert result["actions"][0]["owner"] == "Mehmet"
    assert len(result["rejected"]) == 3


def test_different_speaker_drops_owner_and_keeps_both_raw_proposal_and_reason_distinct() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Bütçeyi kontrol edeceğim.", "S2")])
    item = ContextAction(
        task_source=1,
        owner_source=0,
        owner="Mehmet",
        due_date=None,
        relationship="subject_continuation",
    )
    result = evaluate_candidate(snapshot, ContextSelection(action_items=[item]))
    assert item.owner == "Mehmet"
    assert result["actions"][0]["owner"] is None
    assert result["rejected"] == ["different-speaker-or-scope"]
