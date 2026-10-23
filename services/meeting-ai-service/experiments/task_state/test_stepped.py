"""Sequential candidate contracts. No network or model qualification."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from experiments.task_state.prototype import CandidateLedger, InvalidProposalError, Source
from experiments.task_state.stepped import Step, StepOutput, prompt


def output(kind="create", **fields):
    return StepOutput.model_validate(
        {
            "status": "events",
            "events": [
                {
                    "kind": kind,
                    "target": 0,
                    "work": None,
                    "owner": None,
                    "date": None,
                    "time": None,
                    **fields,
                }
            ],
        }
    )


def test_sequential_state_preserves_omitted_tasks_date_and_cancellation():
    full = Source.parse(
        "Derya raporu 3 Ekim saat 9'a kadar hazırlayacak. "
        "Emre bütçeyi kontrol edecek. Raporun teslim saati 11 olacak. "
        "Bütçe kontrolünü iptal ediyoruz. Ekip başka konuları konuştu."
    )
    ledger = CandidateLedger()
    Step.build(full, ledger).apply(
        ledger,
        output(
            work="raporu",
            owner="Derya",
            date="3 Ekim",
            time="9",
        ),
    )
    original_id = next(iter(ledger.tasks))
    Step.build(full, ledger).apply(ledger, output(work="bütçeyi", owner="Emre"))
    Step.build(full, ledger).apply(ledger, output("reschedule", target=1, time="11"))
    Step.build(full, ledger).apply(ledger, output("cancel", target=2))
    Step.build(full, ledger).apply(ledger, StepOutput(status="no_event", events=[]))
    assert len(ledger.tasks) == 2 and ledger.tasks[original_id].time.text == "11"
    assert ledger.tasks[original_id].date.text == "3 Ekim"
    assert list(ledger.tasks.values())[1].status == "cancelled"
    assert ledger.source.text == full.text


def test_split_owner_quote_is_bound_to_original_characters():
    full, ledger = Source.parse("Aylin. Sunum dosyasını hazırlayacak."), CandidateLedger()
    Step.build(full, ledger).apply(ledger, StepOutput(status="no_event", events=[]))
    Step.build(full, ledger).apply(
        ledger,
        output(
            work="Sunum dosyasını",
            owner="Aylin",
            context_fields=["owner"],
        ),
    )
    task = next(iter(ledger.tasks.values()))
    assert task.owner.start == 0 and full.text[task.owner.start : task.owner.end] == "Aylin"
    assert task.description.start == 7


@pytest.mark.parametrize("quote", ["Ece", "CAN", "olmayan"])
def test_no_approximate_or_repeated_quote_selection(quote):
    step = Step.build(Source.parse("Ece Ece Canlı raporu hazırlayacak."), CandidateLedger())
    with pytest.raises(InvalidProposalError, match="visible_quote_missing_or_ambiguous"):
        step.quote(quote)


def test_valid_first_event_is_not_applied_when_second_target_is_invalid():
    ledger = CandidateLedger()
    full = Source.parse("Ece raporu hazırlayacak ve bütçeyi iptal edecek.")
    valid = output(work="raporu", owner="Ece")
    invalid = output("cancel", target=1)
    with pytest.raises(InvalidProposalError, match="step_unknown_target"):
        Step.build(full, ledger).apply(
            ledger,
            StepOutput(
                status="events",
                events=valid.events + invalid.events,
            ),
        )
    assert not ledger.tasks and ledger.source is None


def test_old_reply_cannot_overwrite_new_state():
    ledger = CandidateLedger()
    step = Step.build(Source.parse("Ece raporu hazırlayacak."), ledger)
    result = output(work="raporu", owner="Ece")
    step.apply(ledger, result)
    before = deepcopy(ledger)
    with pytest.raises(InvalidProposalError, match="step_state_mismatch"):
        step.apply(ledger, result)
    assert ledger == before


def test_source_correction_requires_explicit_replay():
    ledger = CandidateLedger()
    Step.build(Source.parse("Ece raporu hazırlayacak."), ledger).apply(
        ledger,
        output(work="raporu", owner="Ece"),
    )
    with pytest.raises(InvalidProposalError, match="source_revision_requires_replay"):
        Step.build(Source.parse("Derya raporu hazırlayacak. Devam ediyoruz."), ledger)


def test_no_event_advances_coverage_without_changing_tasks_and_unresolved_is_distinct():
    ledger = CandidateLedger()
    full = Source.parse("Toplantı başladı. Onun görevini iptal ediyoruz.")
    Step.build(full, ledger).apply(ledger, StepOutput(status="no_event", events=[]))
    assert len(ledger.source.units) == 1 and not ledger.unresolved
    Step.build(full, ledger).apply(ledger, StepOutput(status="unresolved", events=[]))
    assert len(ledger.source.units) == 2 and ledger.unresolved == {2} and not ledger.tasks


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "create", "target": 1, "work": "raporu"},
        {"kind": "create", "work": None},
        {"kind": "reschedule", "target": 1},
        {"kind": "reschedule", "target": 1, "time": "11", "owner": "Ece"},
        {"kind": "reassign", "target": 1},
        {"kind": "cancel", "target": 1, "work": "raporu"},
        {"kind": "create", "target": True, "work": "raporu"},
        {"kind": "create", "work": " "},
    ],
)
def test_operation_shape_validation(fields):
    with pytest.raises(ValidationError):
        output(**fields)


def test_prompt_contains_no_future_source_or_expected_results():
    full = Source.parse("Ece raporu hazırlayacak. Daha sonra gizli değişiklik gelecek.")
    step = Step.build(full, CandidateLedger())
    text = prompt(step)
    assert "Ece raporu hazırlayacak." in text and "gizli" not in text
    assert "expected" not in text and "GÖREVLER=[]" in text
    assert step.source.text == full.units[0].text


def test_reassignment_does_not_overwrite_other_task_of_same_owner():
    full, ledger = (
        Source.parse(
            "Ece raporu hazırlayacak. Ece bütçeyi kontrol edecek. Raporu Derya'ya veriyoruz."
        ),
        CandidateLedger(),
    )
    Step.build(full, ledger).apply(ledger, output(work="raporu", owner="Ece"))
    Step.build(full, ledger).apply(ledger, output(work="bütçeyi", owner="Ece"))
    Step.build(full, ledger).apply(ledger, output("reassign", target=1, owner="Derya"))
    tasks = list(ledger.tasks.values())
    assert len(tasks) == 2 and tasks[0].owner.text == "Derya" and tasks[1].owner.text == "Ece"


def test_previous_scope_requires_explicit_selection_and_never_falls_back():
    full, ledger = Source.parse("Aylin. Sunum dosyasını hazırlayacak."), CandidateLedger()
    Step.build(full, ledger).apply(ledger, StepOutput(status="no_event", events=[]))
    step = Step.build(full, ledger)
    with pytest.raises(InvalidProposalError, match="visible_quote_missing_or_ambiguous"):
        step.apply(ledger, output(work="Sunum dosyasını", owner="Aylin"))
    assert not ledger.tasks


def test_prompt_exposes_the_same_schema_used_for_decoding():
    step = Step.build(Source.parse("Ece raporu hazırlayacak."), CandidateLedger())
    rendered = prompt(step)
    embedded = rendered.split("\nYANIT ŞEMASI=", 1)[1].split("\nGÖREVLER=", 1)[0]
    assert json.loads(embedded) == step.schema()
    assert "kind" in embedded and "context_fields" in embedded


def test_first_step_cannot_generate_a_reference_to_nonexistent_previous_context():
    full, ledger = Source.parse("Aylin. Sunum dosyasını hazırlayacak."), CandidateLedger()
    first = Step.build(full, ledger)
    assert all(
        variant["properties"]["context_fields"]["maxItems"] == 0
        for variant in first.schema()["$defs"]["Change"]["anyOf"]
    )
    first.apply(ledger, StepOutput(status="no_event", events=[]))
    second = Step.build(full, ledger)
    assert any(
        variant["properties"]["context_fields"]["maxItems"] > 0
        for variant in second.schema()["$defs"]["Change"]["anyOf"]
    )
    second.apply(ledger, output(work="Sunum dosyasını", owner="Aylin", context_fields=["owner"]))
    assert next(iter(ledger.tasks.values())).owner.text == "Aylin"
