import pytest

from experiments.task_state.prototype import CandidateLedger, Proposal, Source
from experiments.task_state.quote_diagnostics import classify, diagnose
from experiments.task_state.stepped import Change, Step, StepOutput


@pytest.mark.parametrize(
    "quote,previous,expected",
    [
        ("raporu", False, "selected_scope_unique"),
        ("Aylin", False, "wrong_scope"),
        ("Aylin", True, "selected_scope_unique"),
        ("raporu", True, "wrong_scope"),
        ("rapor", False, "selected_scope_boundary_only"),
        ("yok", False, "missing"),
        ("not", False, "selected_scope_ambiguous"),
    ],
)
def test_diagnostics_classify_literal_matches_without_changing_validator(quote, previous, expected):
    ledger = CandidateLedger()
    ledger.apply(Source.parse("Aylin."), Proposal(events=[], unresolved=[]))
    step = Step.build(Source.parse("Aylin. raporu not not hazırlayacak."), ledger)
    before = ledger.prompt_state()
    assert classify(step, quote, previous=previous) == expected
    assert ledger.prompt_state() == before


def test_diagnostic_rows_never_include_candidate_or_source_content():
    step = Step.build(Source.parse("PrivateName secretAssignment hazırlayacak."), CandidateLedger())
    result = diagnose(
        step,
        StepOutput(
            status="events",
            events=[
                Change(
                    kind="create",
                    target=0,
                    work="secretAssignment",
                    owner="PrivateName",
                    date=None,
                    time=None,
                    context_fields=["owner"],
                )
            ],
        ),
    )
    assert result == [
        {"event_index": 0, "field": "work", "scope": "current", "reason": "selected_scope_unique"},
        {"event_index": 0, "field": "owner", "scope": "previous", "reason": "wrong_scope"},
    ]
