"""Frozen boundary labels, independent of candidate eligibility or model output."""

from experiments.owner_context.candidate import Snapshot

REMOVE_AFTER = {
    "normal": "Mehmet",
    "zeynep-separated": "Zeynep",
    "surname-separated": "Sevil Karakaş",
    "long-pause": "Mehmet",
}
KEEP = {
    "paused",
    "non-name-fragment",
    "explicit-other-owner",
    "question-after-name",
    "explicit-owner",
    "vocative-first-person",
    "unrelated-name",
    "different-speaker",
    "cancelled-task",
    "reassigned-task",
    "standalone-answer",
}


def expected_positions(case_id: str, snapshot: Snapshot) -> tuple[int, ...]:
    if case_id in KEEP:
        return ()
    fragment = REMOVE_AFTER[case_id] + "."
    if snapshot.text.count(fragment) != 1:
        raise ValueError("gold-anchor-missing-or-ambiguous")
    return (snapshot.text.index(fragment) + len(fragment) - 1,)


def acceptance_passed(behavior: bool, source_stable: bool, model_stable: bool) -> bool:
    return behavior and source_stable and model_stable
