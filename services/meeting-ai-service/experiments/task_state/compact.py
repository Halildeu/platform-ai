"""Local candidate wire format: omit null fields, keep existing evidence checks.

Not imported by the application. Shorter output is a cost experiment, not a
semantic guarantee. Invalid proposals cannot partially mutate the task ledger.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from experiments.task_state.indexed import (
    INSTRUCTION,
    TOKEN,
    IndexedEvent,
    IndexedProposal,
    IndexedRequest,
    PositiveInt,
    Span,
    apply_indexed,
)
from experiments.task_state.prototype import CandidateLedger


class CompactFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    a: PositiveInt
    s: list[PositiveInt] = Field(min_length=1, max_length=16)
    x: PositiveInt | None = None
    w: Span | None = None
    o: Span | None = None
    d: Span | None = None
    t: Span | None = None


class CompactCreate(CompactFields):
    k: Literal["create"]
    x: None = None
    w: Span


class CompactReassign(CompactFields):
    k: Literal["reassign"]
    x: PositiveInt
    w: None = None
    o: Span
    d: None = None
    t: None = None


class CompactTimeChange(CompactFields):
    k: Literal["reschedule"]
    x: PositiveInt
    w: None = None
    o: None = None
    t: Span


class CompactDateChange(CompactFields):
    k: Literal["reschedule"]
    x: PositiveInt
    w: None = None
    o: None = None
    d: Span


class CompactStatusChange(CompactFields):
    k: Literal["cancel", "complete", "reopen"]
    x: PositiveInt
    w: None = None
    o: None = None
    d: None = None
    t: None = None


# anyOf rather than a k-only discriminator: both reschedule variants deliberately
# share the operation name. At least one non-null date/time is required in the
# JSON grammar itself, not just a post-generation Python validator.
CompactEvent: TypeAlias = (
    CompactCreate | CompactReassign | CompactTimeChange | CompactDateChange | CompactStatusChange
)


class CompactProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    e: list[CompactEvent] = Field(max_length=128)
    u: list[PositiveInt] = Field(max_length=128)

    def indexed(self) -> IndexedProposal:
        return IndexedProposal(
            events=[
                IndexedEvent(
                    op=row.k,
                    target=row.x,
                    at=row.a,
                    support=row.s,
                    work=row.w,
                    owner=row.o,
                    date=row.d,
                    time=row.t,
                )
                for row in self.e
            ],
            unresolved=self.u,
        )


def apply_compact(
    ledger: CandidateLedger, request: IndexedRequest, proposal: CompactProposal
) -> None:
    apply_indexed(
        ledger,
        request,
        proposal.indexed(),
        source_sha256=request.source_sha256,
        state_sha256=request.state_sha256,
    )


def build_compact_prompt(request: IndexedRequest) -> str:
    # Change only the representation. The relation rules and source/alias
    # semantics are inherited verbatim from the previously reviewed candidate.
    units = []
    for i, unit in enumerate(request.source.units, 1):
        words = " ".join(f"{n}:{m.group()}" for n, m in enumerate(TOKEN.finditer(unit.text), 1))
        units.append(f"[{i}] {words}")
    mapping = (
        "\nCOMPACT OUTPUT: Use ONLY these short JSON keys: e=events, u=unresolved. "
        "Within each event: k=op, x=target, a=at, s=support, w=work, o=owner, "
        "d=date, t=time. Omit optional null fields x/w/o/d/t. "
        "Keep the original operation names and exact [unit,firstToken,lastToken] ranges. "
        "Do not output the long key names. Both e and u are required.\n"
    )
    return INSTRUCTION + mapping + "TASKS=" + request.state_json + "\nSOURCE:\n" + "\n".join(units)
