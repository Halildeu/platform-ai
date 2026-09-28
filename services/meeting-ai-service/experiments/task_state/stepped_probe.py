"""Fixed synthetic sequential LOCAL evaluation. No production or app integration."""

from __future__ import annotations

import argparse
import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from experiments.task_state.bounded_inference import generate
from experiments.task_state.compact_probe import ALTERNATIVE, require_cpu_runtime
from experiments.task_state.indexed_probe import fingerprint, progress
from experiments.task_state.probe import cases, compare, expected
from experiments.task_state.prototype import CandidateLedger, InvalidProposalError, Source, digest
from experiments.task_state.quote_diagnostics import diagnose
from experiments.task_state.stepped import Step, StepOutput, prompt

HERE = Path(__file__).resolve().parent
OPTIONS = {
    "num_ctx": 8192,
    "num_predict": 384,
    "temperature": 0.0,
    "seed": 42,
    "top_p": 0.9,
    "num_gpu": 0,
    "presence_penalty": 0.0,
}


def selected_cases():
    return [
        (
            "general-smoke",
            "smoke",
            "Aylin raporu hazırlayacak. Kerem bütçeyi kontrol edecek.",
            [expected(["rapor"], "Aylin"), expected(["bütçe"], "Kerem")],
        ),
        *cases(),
    ]


def hashes():
    return {
        name: digest((HERE / name).read_text(encoding="utf-8"))
        for name in (
            "stepped.py",
            "stepped_probe.py",
            "bounded_inference.py",
            "quote_diagnostics.py",
            "prototype.py",
            "indexed.py",
            "indexed_probe.py",
            "compact_probe.py",
            "probe.py",
            "reference.json",
        )
    }


def run(model=ALTERNATIVE, suite="all"):
    if model not in (ALTERNATIVE, "llama3.1:8b") or suite not in ("all", "smoke", "frozen"):
        raise InvalidProposalError("unsupported_probe_profile")
    think = False if model == ALTERNATIVE else None
    report = {
        "schema": "sequential-task-events-local-v1",
        "utc": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "target": "local-loopback-not-TEST",
        "model": model,
        "suite": suite,
        "options": OPTIONS,
        "think": think,
        "source_hashes": hashes(),
        "rows": [],
        "semantic_qualification": False,
        "phone_acceptance": False,
        "calendar_normalization": False,
        "server_context_coverage_verified": False,
    }
    started = time.monotonic()
    selected = selected_cases()
    if suite == "smoke":
        selected = selected[:1]
    elif suite == "frozen":
        selected = selected[1:]
    ledgers = {}
    with httpx.Client(base_url="http://127.0.0.1:11434", timeout=30, trust_env=False) as client:
        report["fingerprint_before"] = fingerprint(client, model)
        if model == ALTERNATIVE:
            info = client.post("/api/show", json={"model": model})
            info.raise_for_status()
            if "thinking" not in info.json().get("capabilities", []):
                raise InvalidProposalError("thinking_control_not_supported")
        for case_id, scenario, text, oracle in selected:
            candidate = copy.deepcopy(ledgers.get(scenario, CandidateLedger()))
            full = Source.parse(text, session=scenario)
            row = {"case": case_id, "source_sha256": digest(text), "steps": []}
            failure_stage = "prepare"
            try:
                while not candidate.source or len(candidate.source.units) < len(full.units):
                    remaining = int(900 - (time.monotonic() - started))
                    if remaining < 1:
                        raise InvalidProposalError("run_time_budget")
                    if hashes() != report["source_hashes"]:
                        raise InvalidProposalError("source_code_drift")
                    step = Step.build(full, candidate)
                    schema = step.schema()
                    rendered = prompt(step, schema)
                    call_started = time.monotonic()
                    call_budget = min(90, remaining)
                    client.timeout = httpx.Timeout(call_budget)
                    call = {"focus": step.focus, "prompt_sha256": digest(rendered)}
                    row["steps"].append(call)
                    failure_stage = "generation"
                    reply = generate(
                        client,
                        model,
                        rendered,
                        schema=schema,
                        options=OPTIONS,
                        think=think,
                        deadline_seconds=call_budget,
                    )
                    failure_stage = "runtime_identity"
                    require_cpu_runtime(client, model)
                    call.update(
                        {
                            "response_sha256": digest(reply["response"]),
                            "seconds": round(time.monotonic() - call_started, 3),
                            "prompt_tokens": reply.get("prompt_eval_count"),
                            "output_tokens": reply.get("eval_count"),
                            "done_reason": reply.get("done_reason"),
                        }
                    )
                    failure_stage = "schema"
                    proposal = StepOutput.model_validate_json(reply["response"])
                    failure_stage = "source_and_state"
                    try:
                        step.apply(candidate, proposal)
                    except InvalidProposalError:
                        row["quote_diagnostics"] = diagnose(step, proposal)
                        raise
                    call.update({"status": proposal.status, "events": len(proposal.events)})
                    progress(
                        {
                            "case": case_id,
                            "step": step.focus,
                            "status": proposal.status,
                            "seconds": call["seconds"],
                        }
                    )
                    if proposal.status == "unresolved":
                        raise InvalidProposalError("unresolved_requires_replay")
                row.update(compare(candidate, oracle))
                row["status"] = "evaluated"
                if row["candidate_relation_pass"]:
                    ledgers[scenario] = candidate
            except Exception as exc:  # noqa: BLE001 — metadata only
                row.update(
                    {
                        "status": "error",
                        "error_class": type(exc).__name__,
                        "failure_stage": failure_stage,
                    }
                )
                if isinstance(exc, InvalidProposalError):
                    row["reason_code"] = str(exc)
            report["rows"].append(row)
            progress(
                {
                    key: row.get(key)
                    for key in (
                        "case",
                        "status",
                        "candidate_relation_pass",
                        "reason_code",
                    )
                }
            )
            if not row.get("candidate_relation_pass"):
                report["stopped_after_failure"] = True
                break
        try:
            report["fingerprint_after"] = fingerprint(client, model)
        except Exception as exc:  # noqa: BLE001
            report["fingerprint_after"] = {"error_class": type(exc).__name__}
    report["source_hashes_after"] = hashes()
    report["code_stable"] = report["source_hashes"] == report["source_hashes_after"]
    report["fingerprint_stable"] = report["fingerprint_before"] == report["fingerprint_after"]
    report["local_relation_cases_passed"] = (
        len(report["rows"]) == len(selected)
        and all(row.get("candidate_relation_pass") for row in report["rows"])
        and report["code_stable"]
        and report["fingerprint_stable"]
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=(ALTERNATIVE, "llama3.1:8b"), default=ALTERNATIVE)
    parser.add_argument("--suite", choices=("all", "smoke", "frozen"), default="all")
    args = parser.parse_args()
    result = run(args.model, args.suite)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return 0 if result["local_relation_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
