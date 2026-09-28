"""Wire compatibility/integrity tests, never evidence of model understanding."""

import pytest
from pydantic import ValidationError

from experiments.task_state.compact import CompactProposal, apply_compact, build_compact_prompt
from experiments.task_state.indexed import IndexedRequest
from experiments.task_state.prototype import CandidateLedger, InvalidProposalError, Source


def fixture():
    source = Source.parse("Ece raporu hazırlayacak. Teslim saati 11 olacak.")
    ledger = CandidateLedger()
    return ledger, IndexedRequest.build(source, ledger)


def test_omitted_null_fields_decode_without_changing_evidence_or_identity():
    ledger, request = fixture()
    proposal = CompactProposal.model_validate(
        {"e": [{"k": "create", "a": 1, "s": [1], "w": [1, 2, 3], "o": [1, 1, 1]}], "u": []}
    )
    apply_compact(ledger, request, proposal)
    task = next(iter(ledger.tasks.values()))
    assert task.description.text == "raporu hazırlayacak"
    assert task.owner.text == "Ece"
    assert task.date is None and task.time is None
    assert task.owner.start == 0


def test_contextual_update_uses_same_target_and_retains_original_description():
    ledger, request = fixture()
    proposal = CompactProposal.model_validate(
        {
            "e": [
                {"k": "create", "a": 1, "s": [1], "w": [1, 2, 3], "o": [1, 1, 1]},
                {"k": "reschedule", "x": 1, "a": 2, "s": [1, 2], "t": [2, 3, 3]},
            ],
            "u": [],
        }
    )
    apply_compact(ledger, request, proposal)
    assert len(ledger.tasks) == 1
    task = next(iter(ledger.tasks.values()))
    assert task.time.text == "11" and task.owner.text == "Ece"
    assert task.description.text == "raporu hazırlayacak"


@pytest.mark.parametrize("field,value", [("x", True), ("a", "1"), ("w", [1, 2]), ("k", "invent")])
def test_malformed_fields_are_not_coerced(field, value):
    row = {"k": "create", "a": 1, "s": [1], "w": [1, 2, 3]}
    row[field] = value
    with pytest.raises(ValidationError):
        CompactProposal.model_validate({"e": [row], "u": []})


def test_invalid_late_target_cannot_partially_apply_valid_create():
    ledger, request = fixture()
    proposal = CompactProposal.model_validate(
        {
            "e": [
                {"k": "create", "a": 1, "s": [1], "w": [1, 2, 3]},
                {"k": "cancel", "x": 99, "a": 2, "s": [2]},
            ],
            "u": [],
        }
    )
    with pytest.raises(InvalidProposalError, match="indexed_unknown_target"):
        apply_compact(ledger, request, proposal)
    assert not ledger.tasks and ledger.source is None


def test_stale_request_cannot_overwrite_new_state():
    ledger, request = fixture()
    proposal = CompactProposal.model_validate(
        {"e": [{"k": "create", "a": 1, "s": [1], "w": [1, 2, 3]}], "u": []}
    )
    apply_compact(ledger, request, proposal)
    with pytest.raises(InvalidProposalError, match="indexed_state_mismatch"):
        apply_compact(ledger, request, proposal)
    assert len(ledger.tasks) == 1


def test_prompt_keeps_source_tokens_and_full_existing_relation_rules():
    _, request = fixture()
    prompt = build_compact_prompt(request)
    assert "An omitted task is unchanged" in prompt
    assert "1:Ece 2:raporu 3:hazırlayacak 4:." in prompt
    assert "e=events, u=unresolved" in prompt
    assert "TASKS=[]" in prompt


@pytest.mark.parametrize(
    "event",
    [
        {"k": "create", "x": 1, "w": [1, 2, 3]},
        {"k": "create"},
        {"k": "reassign", "x": 1},
        {"k": "reassign", "x": 1, "o": [1, 1, 1], "t": [1, 2, 2]},
        {"k": "reschedule", "x": 1},
        {"k": "reschedule", "t": [1, 2, 2]},
        {"k": "cancel", "x": 1, "w": [1, 2, 3]},
        {"k": "complete", "x": 1, "o": [1, 1, 1]},
        {"k": "reopen"},
    ],
)
def test_operation_shapes_are_enforced_before_the_reducer(event):
    with pytest.raises(ValidationError):
        CompactProposal.model_validate({"e": [{"a": 1, "s": [1], **event}], "u": []})


@pytest.mark.parametrize(
    "fields",
    [
        {"t": [1, 2, 2]},
        {"d": [1, 2, 2]},
        {"d": [1, 2, 2], "t": [1, 3, 3]},
    ],
)
def test_reschedule_accepts_date_or_time_or_both_without_clearing_other_fields(fields):
    proposal = CompactProposal.model_validate(
        {
            "e": [{"k": "reschedule", "x": 1, "a": 1, "s": [1], **fields}],
            "u": [],
        }
    )
    converted = proposal.indexed().events[0]
    assert converted.work is None and converted.owner is None
    assert converted.date == fields.get("d") and converted.time == fields.get("t")
