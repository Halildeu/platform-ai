"""Bounded loopback-only classifier + unchanged analyzer evaluation.

Only synthetic fixtures. No deployment, credentials, new models or shared hosts.
Stops at the first failed boundary/analysis gate; no prompt tuning in this run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.services.analyze import MeetingAnalysisService, OllamaAnalyzer  # noqa: E402
from app.services.redact import redact_pii  # noqa: E402
from experiments.owner_context.boundary_gold import (  # noqa: E402
    acceptance_passed,
    expected_positions,
)
from experiments.owner_context.candidate import Snapshot, digest  # noqa: E402
from experiments.owner_context.local_compare import (  # noqa: E402
    HOST,
    MODEL,
    MODEL_DIGEST,
    OPTIONS,
    LocalSettings,
    artificial,
    cases,
    grade,
)
from experiments.owner_context.option_catalog import prepare_options  # noqa: E402
from experiments.owner_context.punctuation_projection import (  # noqa: E402
    BoundarySelection,
    project,
    prompt,
    schema,
    source_map,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
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

    def audit(event: str, values: tuple[Any, ...]) -> None:
        if event == "socket.connect" and values[1] != ("127.0.0.1", 11434):
            raise RuntimeError("non-local-connect")
        if event == "socket.getaddrinfo" and (values[0], values[1]) != ("127.0.0.1", 11434):
            raise RuntimeError("non-local-dns")
        if event in {"socket.bind", "socket.sendto", "subprocess.Popen", "os.system"}:
            raise RuntimeError("external-io-disabled")

    sys.addaudithook(audit)
    watched = [
        *ROOT.joinpath("app").rglob("*.py"),
        *Path(__file__).parent.glob("*.py"),
        *Path(__file__).parent.joinpath("fixtures").glob("*.json"),
    ]

    def hashes() -> dict[str, str]:
        return {str(path.relative_to(ROOT)): digest(path.read_text("utf-8")) for path in watched}

    report: dict[str, Any] = {
        "scope": "isolated-synthetic-punctuation-projection-not-TEST-or-phone",
        "model": MODEL,
        "modelDigest": MODEL_DIGEST,
        "options": OPTIONS,
        "sameAsTESTModel": False,
        "deployed": False,
        "rows": [],
        "startSourceHashes": hashes(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")

    settings = LocalSettings(
        app_env="dev",
        backend="ollama",
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
    calls: list[dict[str, Any]] = []

    def restrict(request: httpx.Request) -> None:
        if str(request.url) != HOST + request.url.path or (
            request.method,
            request.url.path,
        ) not in {
            ("GET", "/api/tags"),
            ("GET", "/api/ps"),
            ("GET", "/api/version"),
            ("POST", "/api/generate"),
        }:
            raise RuntimeError("endpoint-not-allowed")
        if request.method == "POST":
            body = json.loads(request.content)
            if (
                body.get("model") != MODEL
                or body.get("options") != OPTIONS
                or body.get("keep_alive") != "0s"
            ):
                raise RuntimeError("resource-budget-mismatch")
            calls.append({"request": body})

    def capture(response: httpx.Response) -> None:
        if response.request.method == "POST":
            response.read()
            calls[-1]["response"] = response.json()

    with httpx.Client(
        trust_env=False, timeout=180, event_hooks={"request": [restrict], "response": [capture]}
    ) as client:

        def identity() -> bool:
            return [
                m["digest"]
                for m in client.get(HOST + "/api/tags").json()["models"]
                if m["name"] == MODEL
            ] == [MODEL_DIGEST]

        if not identity():
            raise RuntimeError("model-identity-mismatch")
        report["loadedBefore"] = client.get(HOST + "/api/ps").json()
        if report["loadedBefore"].get("models"):
            raise RuntimeError("local-runtime-busy")
        report["runtimeVersion"] = client.get(HOST + "/api/version").json()
        service = MeetingAnalysisService(settings, analyzer=OllamaAnalyzer(settings, client=client))
        by_id = {case["id"]: case for case in cases()}
        by_id["standalone-answer"] = {
            "snapshot": artificial(
                [
                    ("Kim bu görevi yapacak?", "S1"),
                    ("Mehmet.", "S1"),
                    ("Rapor yarın hazır olacak.", "S1"),
                ]
            ),
            "expected": [],
        }
        order = [
            "normal",
            "zeynep-separated",
            "vocative-first-person",
            "non-name-fragment",
            "explicit-other-owner",
            "question-after-name",
            "standalone-answer",
            "long-pause",
            "surname-separated",
            "different-speaker",
            "unrelated-name",
            "cancelled-task",
            "reassigned-task",
            "paused",
            "explicit-owner",
        ]
        passed = True
        for case_id in order:
            case = by_id[case_id]
            snapshot: Snapshot = case["snapshot"]
            if redact_pii(snapshot.text)[0] != snapshot.text:
                raise RuntimeError("redaction-requires-separate-offset-map")
            options = prepare_options(snapshot)
            gold_positions = expected_positions(case_id, snapshot)
            expected_ids = [
                o.option_id
                for o in options
                if snapshot.sources[o.owner_source].char_end - 1 in gold_positions
            ]
            modes = ["manual-upper-bound", "classifier"] if case_id == "normal" else ["classifier"]
            for mode in modes:
                started = time.monotonic()
                calls.clear()
                row: dict[str, Any] = {
                    "case": case_id,
                    "mode": mode,
                    "original": snapshot.text,
                    "originalSha256": snapshot.sha256,
                    "expectedBoundaryIds": expected_ids,
                    "expectedBoundaryPositions": gold_positions,
                    "snapshotMetadata": [asdict(s) for s in snapshot.spans],
                }
                if mode == "manual-upper-bound":
                    selection = BoundarySelection(remove_full_stops=expected_ids)
                elif options:
                    response = client.post(
                        HOST + "/api/generate",
                        json={
                            "model": MODEL,
                            "prompt": prompt(snapshot),
                            "stream": False,
                            "format": schema(snapshot),
                            "options": OPTIONS,
                            "keep_alive": "0s",
                        },
                    )
                    response.raise_for_status()
                    selection = BoundarySelection.model_validate_json(response.json()["response"])
                else:
                    selection = BoundarySelection(remove_full_stops=[])
                projected = project(snapshot, selection)
                mapping = source_map(snapshot, projected)
                row.update(
                    projection=asdict(projected),
                    sourceMap=mapping,
                    chosen=selection.model_dump(),
                    boundaryPassed=projected.changed_positions == gold_positions,
                )
                if projected.changed_positions:
                    if redact_pii(projected.text)[0] != projected.text:
                        raise RuntimeError("projection-redaction-drift")
                    result = service.analyze(projected.text, live=True)
                    expected = []
                    for action in case["expected"]:
                        matches = [
                            m
                            for m in mapping
                            if any(s["text"] == action["text"] for s in m["originalSources"])
                        ]
                        if len(matches) != 1:
                            raise RuntimeError("ambiguous-expected-task-map")
                        expected.append({**action, "text": matches[0]["projectionText"]})
                    row.update(
                        actions=[a.model_dump() for a in result.action_items],
                        rejected=[r.model_dump() for r in result.rejected_claims],
                        projectionCitations=[c.model_dump() for c in result.citations],
                    )
                    row["analysisGrade"] = grade(row["actions"], expected)
                else:
                    row["analysisNotRepeated"] = (
                        "identity projection; no downstream behavior changed"
                    )
                row.update(elapsedSeconds=round(time.monotonic() - started, 3), calls=list(calls))
                report["rows"].append(row)
                save()
                sys.stdout.write(
                    json.dumps(
                        {
                            k: row[k]
                            for k in (
                                "case",
                                "mode",
                                "boundaryPassed",
                                "analysisGrade",
                                "elapsedSeconds",
                            )
                            if k in row
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
                if (
                    not row["boundaryPassed"]
                    or not row.get("analysisGrade", {"exact": True})["exact"]
                ):
                    passed = False
                    break
            if not passed:
                break
        report.update(
            completedAcceptance=passed,
            endSourceHashes=hashes(),
            modelIdentityStable=identity(),
            loadedAfter=client.get(HOST + "/api/ps").json(),
        )
        report["sourceHashesStable"] = report["startSourceHashes"] == report["endSourceHashes"]
        report["completedAcceptance"] = acceptance_passed(
            passed, report["sourceHashesStable"], report["modelIdentityStable"]
        )
        save()
        return 0 if report["completedAcceptance"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
