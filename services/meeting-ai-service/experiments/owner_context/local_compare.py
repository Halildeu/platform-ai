"""Real LOCAL-model prequalification. No shared service, model download or deployment.

Only the already installed, pinned llama3.1:8b on 127.0.0.1:11434 is allowed.
This differs from TEST's qwen model; results cannot authorize product promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.services.analyze import MeetingAnalysisService, OllamaAnalyzer  # noqa: E402
from app.services.citation import due_date_supported_by_source, is_groundable_evidence  # noqa: E402
from app.services.ollama_runtime import generate  # noqa: E402
from app.services.redact import redact_pii  # noqa: E402
from experiments.owner_context.candidate import (  # noqa: E402
    FinalEvent,
    OwnerProposal,
    Snapshot,
    from_events,
    prompt,
    resolve,
)

HOST = "http://127.0.0.1:11434"
MODEL = "llama3.1:8b"
MODEL_DIGEST = "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e"
OPTIONS = {
    "temperature": 0.0,
    "top_p": 0.9,
    "seed": 42,
    "num_ctx": 4096,
    "num_predict": 640,
    "num_thread": 4,
    "num_gpu": 0,
}
FIXTURES = Path(__file__).parent / "fixtures"


class LocalSettings(Settings):
    model_config = SettingsConfigDict(env_file=None)

    def ollama_options(self) -> dict[str, object]:
        return dict(OPTIONS)


class ContextAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    task_source: int = Field(ge=0)
    owner_source: int | None
    owner: str | None
    due_date: str | None
    relationship: Literal["explicit", "subject_continuation", "ambiguous", "unrelated", "retracted"]


class ContextSelection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action_items: list[ContextAction] = Field(max_length=10)


def proposal_prompt(snapshot: Snapshot) -> str:
    instructions, data = prompt(snapshot).split("\n", 1)
    instructions = instructions.replace("and transcript_sha256. ", "and due_date. ")
    return (
        "Extract ALL concrete CURRENT outstanding tasks explicitly assigned or committed to. "  # noqa: S608 -- LLM prompt, no SQL
        "Include first-person commitments with owner=null unless a named owner is explicit. "
        "Re-evaluate earlier tasks when later speech cancels, replaces or completes them. "
        "Do not select meeting schedules, questions, proposals, possibilities or cancelled tasks. "
        "An empty action_items list is valid. Return JSON with action_items as a list. "
        "Use original ZERO-BASED source_index values, not display position. "
        "Copy each full due-date phrase VERBATIM from the TASK source, or null if absent. "
        "Do not infer dates. For owner=null also return owner_source=null. "
        "due_date means an actual deadline date/time expression. A task description, verb or "
        "commitment is NOT a due date. Without an explicit deadline, due_date MUST be null. "
        "Define relationship as follows: explicit = the named assignee occurs in the SAME "
        "task source (owner_source equals task_source); subject_continuation = the preceding "
        "standalone name is the subject of this task (owner_source is the preceding source). "
        "For either named-assignee case, copy the name and set the correct integer owner_source. "
        "ambiguous = no supported named assignee, so owner and owner_source are BOTH null. "
        "The Turkish pronouns ben/biz and first-person verb endings do not identify a name. "
        "A name addressed before a first-person promise is not its assignee. "
        + instructions
        + "\n"
        + data
    )


def artificial(parts: list[tuple[str, str]]) -> Snapshot:
    """Text counterexamples have explicitly synthetic timing, not newly recorded audio."""
    events = []
    for seq, (text, speaker) in enumerate(parts):
        events.append(
            FinalEvent.model_validate(
                {
                    "seq": seq,
                    "text": text,
                    "source_start_sample": seq * 17600,
                    "source_end_sample": seq * 17600 + 16000,
                    "speakerAttribution": {
                        "scope": "synthetic-counterexample",
                        "turns": [
                            {
                                "speaker": speaker,
                                "textStart": 0,
                                "textEnd": len(text.encode("utf-16-le")) // 2,
                                "startMs": 0,
                                "endMs": 1000,
                            }
                        ],
                    },
                }
            )
        )
    return from_events(events)


def cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("normal", "paused"):
        fixture = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
        if fixture.get("synthetic") is not True:
            raise ValueError("synthetic-fixture-required")
        snapshot = from_events([FinalEvent.model_validate(e) for e in fixture["finalEvents"]])
        if snapshot.sha256 != fixture["transcriptSha256"]:
            raise ValueError("fixture-hash-mismatch")
        zeynep = next(s for s in snapshot.sources if "hazırlayacak" in s.text)
        mehmet = next(s for s in snapshot.sources if "kontrol edecek" in s.text)
        rows.append(
            {
                "id": name,
                "snapshot": snapshot,
                "kind": "recorded-synthetic-audio-events",
                "expected": [
                    {
                        "text": zeynep.text,
                        "owner": "Zeynep",
                        "due_date": "25 Eylül 2026 günü saat 17'ye kadar",
                    },
                    {
                        "text": mehmet.text,
                        "owner": "Mehmet",
                        "due_date": "26 Eylül 2026 günü saat 12'ye kadar",
                    },
                ],
            }
        )
    text_cases: list[tuple[str, list[tuple[str, str]], list[tuple[int, str | None]]]] = [
        (
            "zeynep-separated",
            [("Zeynep.", "S1"), ("Sunum dosyasını hazırlayacak.", "S1")],
            [(1, "Zeynep")],
        ),
        (
            "surname-separated",
            [("Sevil Karakaş.", "S1"), ("Raporu hazırlayacak.", "S1")],
            [(1, "Sevil Karakaş")],
        ),
        (
            "non-name-fragment",
            [("Tamam.", "S1"), ("Raporu ben hazırlayacağım.", "S1")],
            [(1, None)],
        ),
        (
            "explicit-other-owner",
            [("Mehmet.", "S1"), ("Raporu Ayşe hazırlayacak.", "S1")],
            [(1, "Ayşe")],
        ),
        ("question-after-name", [("Mehmet.", "S1"), ("Bütçeyi sen kontrol eder misin?", "S1")], []),
        (
            "long-pause",
            [("Mehmet.", "S1"), ("Bütçe tablosunu kontrol edecek.", "S1")],
            [(1, "Mehmet")],
        ),
        ("explicit-owner", [("Mehmet bütçe tablosunu kontrol edecek.", "S1")], [(0, "Mehmet")]),
        (
            "vocative-first-person",
            [("Mehmet.", "S1"), ("Bütçe tablosunu ben kontrol edeceğim.", "S1")],
            [(1, None)],
        ),
        (
            "unrelated-name",
            [
                ("Mehmet toplantıya katılmadı.", "S1"),
                ("Bütçe tablosunu ben kontrol edeceğim.", "S1"),
            ],
            [(1, None)],
        ),
        (
            "different-speaker",
            [("Mehmet.", "S1"), ("Bütçe tablosunu kontrol edeceğim.", "S2")],
            [(1, None)],
        ),
        (
            "cancelled-task",
            [
                ("Mehmet raporu hazırlayacak.", "S1"),
                ("Bu görev iptal edildi; rapor hazırlanmayacak.", "S1"),
            ],
            [],
        ),
        (
            "reassigned-task",
            [
                ("Mehmet raporu hazırlayacak.", "S1"),
                ("Bu görev artık Ayşe'ye verildi; raporu Ayşe hazırlayacak.", "S1"),
            ],
            [(1, "Ayşe")],
        ),
    ]
    for case_id, parts, expected in text_cases:
        snapshot = artificial(parts)
        if case_id == "long-pause":
            late = replace(snapshot.spans[1], start_ms=4500, end_ms=5500)
            snapshot = replace(snapshot, spans=(snapshot.spans[0], late))
        rows.append(
            {
                "id": case_id,
                "snapshot": snapshot,
                "kind": "synthetic-text-counterexample",
                "expected": [
                    {"text": snapshot.sources[index].text, "owner": owner, "due_date": None}
                    for index, owner in expected
                ],
            }
        )
    return rows


def grade(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> dict[str, Any]:
    def normalized(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(
            json.dumps(
                {k: row.get(k) for k in ("text", "owner", "due_date")},
                sort_keys=True,
                ensure_ascii=False,
            )
            for row in rows
        )

    named_false_positives = [
        row
        for row in actual
        if row.get("owner")
        and not any(
            row["text"] == gold["text"] and row["owner"] == gold["owner"] for gold in expected
        )
    ]
    return {
        "exact": normalized(actual) == normalized(expected),
        "expectedCount": len(expected),
        "actualCount": len(actual),
        "wrongNamedOwners": len(named_false_positives),
    }


def evaluate_candidate(snapshot: Snapshot, selection: ContextSelection) -> dict[str, Any]:
    actions, evidence, rejected = [], [], []
    seen: set[int] = set()
    for item in selection.action_items:
        if item.task_source in seen or item.task_source >= len(snapshot.sources):
            rejected.append("invalid-or-duplicate-task")
            continue
        seen.add(item.task_source)
        source = snapshot.sources[item.task_source]
        if not is_groundable_evidence(source.text) or item.relationship == "retracted":
            rejected.append("non-task-or-retracted")
            continue
        resolution = resolve(
            snapshot,
            OwnerProposal(
                transcript_sha256=snapshot.sha256,
                task_source=item.task_source,
                owner_source=item.owner_source,
                owner=item.owner,
                relationship=item.relationship,
            ),
        )
        due = item.due_date if due_date_supported_by_source(item.due_date, source.text) else None
        actions.append({"text": source.text, "owner": resolution.owner, "due_date": due})
        evidence.append({"task": asdict(source), "ownerResolution": asdict(resolution)})
        if item.owner and resolution.owner is None:
            rejected.append(resolution.reason)
        if due != item.due_date:
            rejected.append("unsupported-date")
    return {"actions": actions, "evidence": evidence, "rejected": rejected}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=["explicit-owner", "vocative-first-person"])
    parser.add_argument("--variant", choices=["original", "catalog"], default="original")
    args = parser.parse_args()
    for key in list(os.environ):
        if key.upper() not in {
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "PATH",
            "COMSPEC",
            "SYSTEMDRIVE",
        }:
            del os.environ[key]
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    denied: list[str] = []

    def audit(event: str, values: tuple[Any, ...]) -> None:
        allowed = True
        if event == "socket.connect":
            allowed = values[1] == ("127.0.0.1", 11434)
        elif event == "socket.getaddrinfo":
            allowed = values[0] == "127.0.0.1" and values[1] == 11434
        elif event in {"socket.bind", "socket.sendto", "subprocess.Popen", "os.system"}:
            allowed = False
        if not allowed:
            denied.append(event)
            raise RuntimeError("local-only-evaluation-blocked-external-io")

    sys.addaudithook(audit)
    try:
        socket.getaddrinfo("external.invalid", 443)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("io-guard-self-check-failed")
    denied.clear()
    recordings: list[dict[str, Any]] = []

    def restrict(request: httpx.Request) -> None:
        path = request.url.path
        if str(request.url) != HOST + path or (request.method, path) not in {
            ("GET", "/api/tags"),
            ("GET", "/api/ps"),
            ("GET", "/api/version"),
            ("POST", "/api/generate"),
        }:
            raise ValueError("nonlocal-or-unapproved-endpoint")
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("model") != MODEL:
                raise ValueError("unapproved-model")
            if body.get("options") != OPTIONS or body.get("keep_alive") != "0s":
                raise ValueError("unexpected-resource-budget")
            recordings.append({"prompt": body.get("prompt"), "format": body.get("format")})

    def capture(response: httpx.Response) -> None:
        if response.request.method == "POST":
            response.read()
            recordings[-1]["envelope"] = response.json()

    report: dict[str, Any] = {
        "scope": "real-local-model-prequalification-not-TEST-or-phone-acceptance",
        "model": MODEL,
        "modelDigest": MODEL_DIGEST,
        "options": OPTIONS,
        "sameAsTESTModel": False,
        "sharedServerCalled": False,
        "deployed": False,
        "candidateVariant": args.variant,
        "rows": [],
        "blockedIo": denied,
        "startSourceHashes": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("candidate.py", "local_compare.py", "option_catalog.py")
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    settings = LocalSettings(
        app_env="dev",
        backend="ollama",
        redact_pii=True,
        ollama_host=HOST,
        ollama_model=MODEL,
        ollama_expected_digest=MODEL_DIGEST,
        ollama_num_ctx=4096,
        ollama_seed=42,
        ollama_temperature=0,
        ollama_keep_alive="0s",
        request_timeout=180,
        ingestion_enabled=False,
        ready_consumer_enabled=False,
    )
    with httpx.Client(
        trust_env=False, timeout=180, event_hooks={"request": [restrict], "response": [capture]}
    ) as client:
        inventory = client.get(HOST + "/api/tags").json()
        if [m["digest"] for m in inventory["models"] if m["name"] == MODEL] != [MODEL_DIGEST]:
            raise RuntimeError("model-identity-mismatch")
        report["loadedBefore"] = client.get(HOST + "/api/ps").json()
        report["runtimeVersion"] = client.get(HOST + "/api/version").json()
        if report["loadedBefore"].get("models"):
            raise RuntimeError("local-model-runtime-busy")
        service = MeetingAnalysisService(settings, analyzer=OllamaAnalyzer(settings, client=client))
        all_cases = {case["id"]: case for case in cases()}
        for case_id in args.cases:
            case = all_cases[case_id]
            snapshot = case["snapshot"]
            if redact_pii(snapshot.text)[0] != snapshot.text:
                raise ValueError("fixture-redaction-would-change-source-references")
            for arm in ("baseline", "candidate"):
                row: dict[str, Any] = {
                    "case": case_id,
                    "arm": arm,
                    "kind": case["kind"],
                    "transcript": snapshot.text,
                    "inputSha256": snapshot.sha256,
                    "expected": case["expected"],
                }
                started = time.monotonic()
                recordings.clear()
                try:
                    if arm == "baseline":
                        result = service.analyze(snapshot.text, live=True)
                        row.update(
                            actions=[a.model_dump() for a in result.action_items],
                            rejected=[r.model_dump() for r in result.rejected_claims],
                            evidence=[c.model_dump() for c in result.citations],
                        )
                    else:
                        from experiments.owner_context import option_catalog

                        response = generate(
                            settings,
                            {
                                "model": MODEL,
                                "prompt": (
                                    option_catalog.prompt(snapshot)
                                    if args.variant == "catalog"
                                    else proposal_prompt(snapshot)
                                ),
                                "stream": False,
                                "format": (
                                    option_catalog.selection_schema(snapshot)
                                    if args.variant == "catalog"
                                    else ContextSelection.model_json_schema()
                                ),
                                "options": OPTIONS,
                                "keep_alive": "0s",
                            },
                            client=client,
                        )
                        response.raise_for_status()
                        if args.variant == "catalog":
                            catalog_selection = option_catalog.CatalogSelection.model_validate_json(
                                response.json()["response"]
                            )
                            row.update(option_catalog.evaluate(snapshot, catalog_selection))
                        else:
                            selection = ContextSelection.model_validate_json(
                                response.json()["response"]
                            )
                            row.update(evaluate_candidate(snapshot, selection))
                    row["grade"] = grade(row["actions"], case["expected"])
                except (httpx.HTTPError, ValueError, RuntimeError) as error:
                    row["errorType"] = type(error).__name__
                row["elapsedSeconds"] = round(time.monotonic() - started, 3)
                row["calls"] = list(recordings)
                report["rows"].append(row)
                save()
                sys.stdout.write(
                    json.dumps(
                        {
                            k: row[k]
                            for k in ("case", "arm", "elapsedSeconds", "grade", "errorType")
                            if k in row
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
                if "errorType" in row:
                    return 2
        after = client.get(HOST + "/api/tags").json()
        report["modelIdentityStable"] = [
            m["digest"] for m in after["models"] if m["name"] == MODEL
        ] == [MODEL_DIGEST]
        report["sourceHashes"] = {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("candidate.py", "local_compare.py", "option_catalog.py")
        }
        report["loadedAfter"] = client.get(HOST + "/api/ps").json()
        save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
