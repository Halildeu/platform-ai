"""Bounded LOCAL qualification of the compact wire candidate; no deployment.

The first frozen reference case gates all later calls. Every failed semantic or
transport check stops the run, even when the model produced valid JSON. Expected
answers are used only after inference, never put into model prompts.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from experiments.task_state.compact import CompactProposal, apply_compact, build_compact_prompt
from experiments.task_state.indexed import IndexedRequest
from experiments.task_state.indexed_probe import MODELS, fingerprint, generate, progress
from experiments.task_state.probe import cases, compare
from experiments.task_state.prototype import CandidateLedger, InvalidProposalError, Source, digest

MODEL = "qwen2.5:3b-instruct"
OPTIONS = {"num_ctx": 8192, "temperature": 0.0, "top_p": 0.9, "num_predict": 768, "seed": 42}
ALTERNATIVE = "qwen3.5:4b"
ALTERNATIVE_OPTIONS = {
    "num_ctx": 8192,
    "temperature": 0.0,
    "top_p": 0.9,
    "num_predict": 1024,
    "seed": 42,
    "num_gpu": 0,
    "presence_penalty": 0.0,
}
HERE = Path(__file__).resolve().parent


def require_cpu_runtime(client: httpx.Client, model: str) -> None:
    runtime = client.get("/api/ps")
    runtime.raise_for_status()
    selected = [row for row in runtime.json().get("models", []) if row.get("name") == model]
    if (
        len(selected) != 1
        or selected[0].get("digest") != MODELS[model]
        or selected[0].get("size_vram") != 0
        or selected[0].get("context_length") != ALTERNATIVE_OPTIONS["num_ctx"]
    ):
        raise InvalidProposalError("candidate_runtime_mismatch")


def code_hashes() -> dict:
    return {
        name: digest((HERE / name).read_text(encoding="utf-8"))
        for name in (
            "compact.py",
            "compact_probe.py",
            "indexed.py",
            "indexed_probe.py",
            "prototype.py",
            "probe.py",
            "reference.json",
        )
    }


def run(model: str = MODEL) -> dict:
    if model not in (MODEL, ALTERNATIVE):
        raise InvalidProposalError("model_not_allowed")
    options = ALTERNATIVE_OPTIONS if model == ALTERNATIVE else OPTIONS
    think = False if model == ALTERNATIVE else None
    selected = cases()
    report = {
        "schema": "compact-task-events-local-v1",
        "utc": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "target": "local-loopback-not-TEST",
        "semantic_qualification": False,
        "phone_acceptance": False,
        "calendar_normalization": False,
        "server_context_coverage_verified": False,
        "model": model,
        "options": options,
        "think": think,
        "source_hashes": code_hashes(),
        "rows": [],
    }
    ledgers = {}
    started_run = time.monotonic()
    with httpx.Client(base_url="http://127.0.0.1:11434", timeout=30, trust_env=False) as client:
        report["fingerprint_before"] = fingerprint(client, model)
        if model == ALTERNATIVE:
            info = client.post("/api/show", json={"model": model})
            info.raise_for_status()
            if "thinking" not in info.json().get("capabilities", []):
                raise InvalidProposalError("thinking_control_not_supported")
        for case_id, scenario, text, oracle in selected:
            if time.monotonic() - started_run > 600:
                report["budget_exhausted"] = True
                break
            ledger = ledgers.setdefault(scenario, CandidateLedger())
            source = Source.parse(text, session=scenario)
            request = IndexedRequest.build(source, ledger)
            prompt = build_compact_prompt(request)
            row = {
                "case": case_id,
                "source_sha256": digest(text),
                "prompt_sha256": digest(prompt),
                "source_chars": len(text),
                "prompt_chars": len(prompt),
            }
            started = time.monotonic()
            try:
                output = generate(
                    client,
                    model,
                    prompt,
                    schema=CompactProposal.model_json_schema(),
                    options=options,
                    deadline_seconds=120,
                    think=think,
                )
                if model == ALTERNATIVE:
                    require_cpu_runtime(client, model)
                row.update(
                    {
                        "response_sha256": digest(output["response"]),
                        "prompt_tokens": output.get("prompt_eval_count"),
                        "output_tokens": output.get("eval_count"),
                        "done_reason": output.get("done_reason"),
                    }
                )
                proposal = CompactProposal.model_validate_json(output["response"])
                candidate = copy.deepcopy(ledger)
                apply_compact(candidate, request, proposal)
                row.update(compare(candidate, oracle))
                row.update({"status": "evaluated", "events": len(proposal.e)})
                if row["candidate_relation_pass"]:
                    ledgers[scenario] = candidate
            except Exception as exc:  # noqa: BLE001 — metadata only, no raw reply/error prose
                row.update({"status": "error", "error_class": type(exc).__name__})
                if isinstance(exc, InvalidProposalError):
                    row["reason_code"] = str(exc)
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            row["within_five_second_target"] = row["elapsed_seconds"] <= 5
            report["rows"].append(row)
            progress(
                {
                    key: row.get(key)
                    for key in (
                        "case",
                        "status",
                        "reason_code",
                        "candidate_relation_pass",
                        "elapsed_seconds",
                    )
                }
            )
            if not row.get("candidate_relation_pass"):
                report["stopped_after_failure"] = True
                break
        try:
            report["fingerprint_after"] = fingerprint(client, model)
            runtime = client.get("/api/ps")
            runtime.raise_for_status()
            report["runtime_models_after"] = [
                {key: model.get(key) for key in ("name", "digest", "size_vram", "context_length")}
                for model in runtime.json().get("models", [])
            ]
        except Exception as exc:  # noqa: BLE001
            report["fingerprint_after"] = {"error_class": type(exc).__name__}
    report["source_hashes_after"] = code_hashes()
    report["code_stable"] = report["source_hashes"] == report["source_hashes_after"]
    report["fingerprint_stable"] = report["fingerprint_before"] == report["fingerprint_after"]
    runtime_models = report.get("runtime_models_after", [])
    matching_runtime = [row for row in runtime_models if row["name"] == model]
    report["cpu_runtime_verified"] = (
        len(matching_runtime) == 1
        and matching_runtime[0].get("size_vram") == 0
        and matching_runtime[0].get("context_length") == options["num_ctx"]
    )
    report["local_relation_cases_passed"] = (
        len(report["rows"]) == len(selected)
        and all(row.get("candidate_relation_pass") for row in report["rows"])
        and report["code_stable"]
        and report["fingerprint_stable"]
        and (model != ALTERNATIVE or report["cpu_runtime_verified"])
    )
    report["candidate_gate_passed"] = report["local_relation_cases_passed"] and all(
        row["within_five_second_target"] for row in report["rows"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=(MODEL, ALTERNATIVE), default=MODEL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.model)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    progress({key: report[key] for key in ("local_relation_cases_passed", "candidate_gate_passed")})
    return 0 if report["candidate_gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
