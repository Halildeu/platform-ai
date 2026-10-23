"""Metadata-only diagnostics for literal candidate quotations; no repair path."""

from experiments.task_state.prototype import InvalidProposalError, Quote, evidence
from experiments.task_state.stepped import Step, StepOutput


def classify(step: Step, text: str, *, previous: bool) -> str:
    valid: dict[bool, int] = {False: 0, True: 0}
    literal: dict[bool, int] = {False: 0, True: 0}
    for index in range(step.first_visible, step.focus + 1):
        unit = step.source.units[index - 1]
        scope = index < step.focus
        offset = unit.text.find(text)
        while offset >= 0:
            literal[scope] += 1
            try:
                evidence(step.source, Quote(source=index, text=text, offset=offset))
            except InvalidProposalError:
                pass
            else:
                valid[scope] += 1
            offset = unit.text.find(text, offset + 1)
    if valid[previous] == 1:
        return "selected_scope_unique"
    if valid[previous] > 1:
        return "selected_scope_ambiguous"
    if valid[not previous]:
        return "wrong_scope"
    if literal[previous]:
        return "selected_scope_boundary_only"
    return "missing"


def diagnose(step: Step, proposal: StepOutput) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for event_index, event in enumerate(proposal.events):
        for field in ("work", "owner", "date", "time"):
            value = getattr(event, field)
            if value is not None:
                previous = field in event.context_fields
                rows.append(
                    {
                        "event_index": event_index,
                        "field": field,
                        "scope": "previous" if previous else "current",
                        "reason": classify(step, value, previous=previous),
                    }
                )
    return rows
