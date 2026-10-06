"""Candidate reducer integrity. These tests do not establish model accuracy."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from experiments.task_state.prototype import (
    CandidateLedger,
    InvalidProposalError,
    Operation,
    Proposal,
    Quote,
    Source,
    evidence,
)


def operation(op="create", anchor=1, target=None, support=None, **fields):
    return Operation(
        op=op,
        target=target,
        anchor=anchor,
        support=support or [anchor],
        **{k: fields.get(k) for k in ("description", "owner", "date", "time")},
    )


def batch(*ops, unresolved=None):
    return Proposal(events=list(ops), unresolved=unresolved or [])


def quote(text, source=1):
    return Quote(text=text, source=source)


def initial():
    source = Source.parse("Zeynep sunum dosyasını 28 Eylül 2026 saat 10'da hazırlayacak.")
    create = operation(
        description=quote("sunum dosyasını"),
        owner=quote("Zeynep"),
        date=quote("28 Eylül 2026"),
        time=quote("10"),
    )
    ledger = CandidateLedger()
    ledger.apply(source, batch(create))
    return source, create, ledger, next(iter(ledger.tasks))


def test_time_patch_preserves_work_owner_date_and_provenance_across_split():
    source, _, ledger, task_id = initial()
    source = Source.parse(source.text + " Zeynep'in sunum dosyasının saati 10 değil. 11 olacak.")
    patch = operation("reschedule", 3, task_id, [2, 3], time=quote("11", 3))
    ledger.apply(source, batch(patch))
    task = ledger.tasks[task_id]
    assert len(ledger.tasks) == 1
    assert (task.description.text, task.owner.text, task.date.text, task.time.text) == (
        "sunum dosyasını",
        "Zeynep",
        "28 Eylül 2026",
        "11",
    )
    assert task.time.source == 3 and task.owner.source == 1
    assert source.text[task.time.start : task.time.end] == "11"
    assert len(task.history) == 2


def test_omission_and_unresolved_change_never_delete_existing_tasks():
    source, _, ledger, task_id = initial()
    extended = Source.parse(source.text + " Ekip başka konuları görüştü.")
    ledger.apply(extended, batch(unresolved=[2]))
    assert ledger.tasks[task_id].status == "active"
    assert ledger.unresolved == {2}


def test_duplicate_delivery_is_idempotent_even_after_later_patch():
    source, create, ledger, task_id = initial()
    extended = Source.parse(source.text + " Sunum saat 11'de tamamlanacak.")
    patch = operation("reschedule", 2, task_id, time=quote("11", 2))
    ledger.apply(extended, batch(patch))
    expected = deepcopy(ledger)
    ledger.apply(extended, batch(create, patch))
    assert ledger == expected


def test_new_create_payload_cannot_overwrite_original_task():
    source, create, ledger, _ = initial()
    changed = create.model_copy(update={"time": None})
    with pytest.raises(InvalidProposalError, match="conflicting_create"):
        ledger.apply(source, batch(changed))


def test_transfer_changes_only_target_task_and_keeps_same_owner_other_work():
    source = Source.parse("Ayşe listeyi güncelleyecek. Elif görselleri hazırlayacak.")
    ledger = CandidateLedger()
    ledger.apply(
        source,
        batch(
            operation(description=quote("listeyi"), owner=quote("Ayşe")),
            operation(anchor=2, description=quote("görselleri", 2), owner=quote("Elif", 2)),
        ),
    )
    first, second = ledger.tasks
    source = Source.parse(source.text + " Görselleri Elif yerine Ayşe hazırlayacak.")
    ledger.apply(source, batch(operation("reassign", 3, second, owner=quote("Ayşe", 3))))
    assert len(ledger.tasks) == 2
    assert ledger.tasks[first].owner.source == 1
    assert ledger.tasks[second].owner.source == 3


def test_cancellation_tombstone_survives_stale_or_new_schedule_until_explicit_reopen():
    source, create, ledger, task_id = initial()
    source = Source.parse(source.text + " Sunum görevini iptal ettik. Saati 11 olacak.")
    ledger.apply(source, batch(operation("cancel", 2, task_id)))
    ledger.apply(source, batch(create))
    with pytest.raises(InvalidProposalError, match="terminal_task_requires_reopen"):
        ledger.apply(source, batch(operation("reschedule", 3, task_id, time=quote("11", 3))))
    assert ledger.tasks[task_id].status == "cancelled"
    source = Source.parse(source.text + " Sunum görevini yeniden açıyoruz.")
    ledger.apply(source, batch(operation("reopen", 4, task_id)))
    assert ledger.tasks[task_id].status == "active"


@pytest.mark.parametrize("change", ["text", "revision", "session", "shorter"])
def test_source_mutations_require_explicit_fresh_replay(change):
    source, _, ledger, _ = initial()
    modified = {
        "text": Source.parse(source.text.replace("Zeynep", "Mehmet")),
        "revision": Source.parse(source.text, revision="2"),
        "session": Source.parse(source.text, session="other"),
        "shorter": Source.parse("Zeynep sunum dosyasını hazırlayacak."),
    }[change]
    before = deepcopy(ledger)
    with pytest.raises(InvalidProposalError, match="source_revision_requires_replay"):
        ledger.apply(modified, batch())
    assert ledger == before


def test_new_revision_replays_into_new_namespace_not_old_field_history():
    source, create, ledger, old_id = initial()
    revised = Source.parse(source.text.replace("10'da", "11'de"), revision="2")
    replay = CandidateLedger()
    replay.apply(revised, batch(create.model_copy(update={"time": quote("11")})))
    assert old_id not in replay.tasks
    assert len(next(iter(replay.tasks.values())).history) == 1
    assert ledger.tasks[old_id].time.text == "10"


def test_bad_later_event_rolls_back_whole_batch():
    source, _, ledger, task_id = initial()
    source = Source.parse(source.text + " Sunum Ayşe'ye verildi. Saat 11 olacak.")
    before = deepcopy(ledger)
    with pytest.raises(InvalidProposalError, match="quote_missing_or_ambiguous"):
        ledger.apply(
            source,
            batch(
                operation("reassign", 2, task_id, owner=quote("Ayşe", 2)),
                operation("reschedule", 3, task_id, time=quote("12", 3)),
            ),
        )
    assert ledger == before


def test_out_of_order_batch_uses_relation_anchor_not_old_field_reference():
    source, _, ledger, task_id = initial()
    source = Source.parse(source.text + " Saati 11 olacak. Daha sonra saati 12 olacak.")
    ledger.apply(
        source,
        batch(
            operation("reschedule", 3, task_id, [1, 3], time=quote("12", 3)),
            operation("reschedule", 2, task_id, [1, 2], time=quote("11", 2)),
        ),
    )
    assert ledger.tasks[task_id].time.text == "12"


def test_conflicting_same_anchor_is_atomic_failure():
    source, _, ledger, task_id = initial()
    source = Source.parse(source.text + " Sunumu 11 veya 12'de tamamlayabiliriz.")
    before = deepcopy(ledger)
    with pytest.raises(InvalidProposalError, match="stale_or_conflicting_anchor"):
        ledger.apply(
            source,
            batch(
                operation("reschedule", 2, task_id, time=quote("11", 2)),
                operation("reschedule", 2, task_id, time=quote("12", 2)),
            ),
        )
    assert ledger == before


def test_more_than_ten_tasks_are_preserved():
    source = Source.parse(" ".join(f"Ekip rapor {i} hazırlayacak." for i in range(12)))
    ledger = CandidateLedger()
    ledger.apply(
        source,
        batch(
            *[operation(anchor=i + 1, description=quote(f"rapor {i}", i + 1)) for i in range(12)]
        ),
    )
    assert len(ledger.tasks) == 12


def test_raw_multiline_quote_maps_to_original_bytes():
    source = Source.parse("Zeynep\nsunum dosyasını\nhazırlayacak.")
    result = evidence(source, quote("sunum dosyasını"))
    assert source.text[result.start : result.end] == "sunum dosyasını"


def test_same_anchor_disjoint_changes_work_across_batches():
    source, _, ledger, task_id = initial()
    source = Source.parse(source.text + " Sunumu Ayşe hazırlayacak ve saat 11'de teslim edecek.")
    transfer = operation("reassign", 2, task_id, owner=quote("Ayşe", 2))
    schedule = operation("reschedule", 2, task_id, time=quote("11", 2))
    ledger.apply(source, batch(transfer))
    ledger.apply(source, batch(schedule))
    assert ledger.tasks[task_id].owner.text == "Ayşe"
    assert ledger.tasks[task_id].time.text == "11"
    ledger.apply(source, batch(transfer, schedule))
    assert len(ledger.tasks[task_id].history) == 3


def test_complete_eight_to_seven_state_with_explicit_proposals_not_model_output():
    reference = json.loads(Path(__file__).with_name("reference.json").read_text(encoding="utf-8"))
    source = Source.parse(reference["initial"])
    ledger = CandidateLedger()
    create = [
        operation(
            anchor=11,
            support=[3, 11],
            description=quote("sunum dosyasını", 3),
            owner=quote("Zeynep", 3),
            date=quote("28 Eylül 2026", 11),
            time=quote("10", 11),
        ),
        operation(
            anchor=12,
            support=[4, 12],
            description=quote("bütçe tablosunu", 4),
            owner=quote("Mehmet", 4),
            date=quote("aynı gün", 12),
            time=quote("12", 12),
        ),
    ]
    for index, work, owner in (
        (5, "müşteri listesini", "Ayşe Yılmaz"),
        (6, "toplantı raporunu", "Sevil Karakaş"),
        (7, "teklif dosyasını", "Halil Koçoğlu"),
        (8, "toplantı davetini", "Deniz Arslan"),
        (9, "ürün görsellerini", "Elif Demir"),
        (10, "sunum bağlantısını", "Can Kaya"),
    ):
        create.append(
            operation(anchor=index, description=quote(work, index), owner=quote(owner, index))
        )
    ledger.apply(source, batch(*create))
    assert len(ledger.tasks) == 8
    by_owner = {t.owner.text: t.task_id for t in ledger.tasks.values()}
    source = Source.parse(
        source.text + " " + reference["change"].replace("10 değil, 11", "10 değil. 11")
    )
    changes = batch(
        operation("reassign", 15, by_owner["Elif Demir"], owner=quote("Ayşe Yılmaz", 15)),
        operation("cancel", 17, by_owner["Can Kaya"]),
        operation("reschedule", 20, by_owner["Zeynep"], [19, 20], time=quote("11", 20)),
    )
    ledger.apply(source, changes)
    before = deepcopy(ledger.tasks)
    ledger.apply(source, changes)  # duplicate transport delivery
    ledger.apply(source, batch())  # an empty selection is no cancellation
    assert ledger.tasks == before
    active = [t for t in ledger.tasks.values() if t.status == "active"]
    assert len(active) == 7
    assert sum(t.owner.text == "Ayşe Yılmaz" for t in active) == 2
    zeynep = ledger.tasks[by_owner["Zeynep"]]
    assert zeynep.time.text == "11" and zeynep.date.text == "28 Eylül 2026"
    assert ledger.tasks[by_owner["Can Kaya"]].status == "cancelled"


def test_old_create_with_changed_quote_cannot_resurrect_cancelled_work():
    source, create, ledger, task_id = initial()
    source = Source.parse(source.text + " Sunum görevini iptal ediyoruz.")
    ledger.apply(source, batch(operation("cancel", 2, task_id)))
    old_create = create.model_copy(update={"description": quote("Zeynep sunum dosyasını")})
    with pytest.raises(InvalidProposalError, match="historical_create_requires_replay"):
        ledger.apply(source, batch(old_create))
    assert len(ledger.tasks) == 1 and ledger.tasks[task_id].status == "cancelled"


@pytest.mark.parametrize("fragment", ["Can", "1"])
def test_partial_word_or_number_quote_is_not_field_evidence(fragment):
    source = Source.parse("Canlı sunum saat 10'da hazırlanacak.")
    with pytest.raises(InvalidProposalError, match="quote_token_boundary"):
        evidence(source, quote(fragment))


def test_same_anchor_status_change_is_exclusive():
    source, _, ledger, task_id = initial()
    source = Source.parse(source.text + " Sunumu iptal ettik ve sonra yeniden açtık.")
    with pytest.raises(InvalidProposalError, match="stale_or_conflicting_anchor"):
        ledger.apply(
            source,
            batch(
                operation("cancel", 2, task_id),
                operation("reopen", 2, task_id),
            ),
        )
    assert ledger.tasks[task_id].status == "active"


@pytest.mark.parametrize(
    "op,reason",
    [
        (operation(description=quote("X", 2)), "field_not_in_support"),
        (operation(support=[1, 1], description=quote("Zeynep")), "relation_source_bounds"),
        (operation("cancel", target="missing"), "unknown_target_or_replaced_description"),
    ],
)
def test_invalid_relations_reject_without_any_mutation(op, reason):
    ledger = CandidateLedger()
    with pytest.raises(InvalidProposalError, match=reason):
        ledger.apply(Source.parse("Zeynep sunumu hazırlayacak."), batch(op))
    assert ledger.source is None and not ledger.tasks
