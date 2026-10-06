"""Fixed synthetic LOCAL indexed-event evaluation. No service/model installation.

Only the two already installed fingerprinted candidates and fixed synthetic cases
are supported. Output contains aggregate errors/hashes, never source/model prose.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from experiments.task_state.indexed import (
    IndexedProposal,
    IndexedRequest,
    apply_indexed,
    build_indexed_prompt,
)
from experiments.task_state.probe import cases, compare
from experiments.task_state.prototype import CandidateLedger, InvalidProposalError, Source, digest

MODELS = {
    "llama3.1:8b": "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e",
    "qwen2.5:3b-instruct": "357c53fb659c5076de1d65ccb0b397446227b71a42be9d1603d46168015c9e4b",
    "qwen3.5:4b": "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
}
OPTIONS = {"num_ctx": 8192, "temperature": 0.0, "top_p": 0.9, "num_predict": 2048, "seed": 42}
HERE = Path(__file__).resolve().parent


def progress(value: dict) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def fingerprint(client: httpx.Client, model: str) -> dict:
    if model not in MODELS:
        raise InvalidProposalError("model_not_allowed")
    version = client.get("/api/version")
    version.raise_for_status()
    tags = client.get("/api/tags")
    tags.raise_for_status()
    found = [m for m in tags.json()["models"] if m["name"] == model]
    if len(found) != 1 or found[0]["digest"] != MODELS[model]:
        raise InvalidProposalError("model_fingerprint_mismatch")
    return {"version": version.json()["version"], "model": model, "digest": found[0]["digest"]}


def code_hashes() -> dict:
    return {
        name: digest((HERE / name).read_text(encoding="utf-8"))
        for name in ("prototype.py", "indexed.py", "indexed_probe.py", "probe.py", "reference.json")
    }


def generate(
    client: httpx.Client,
    model: str,
    prompt: str,
    *,
    schema: dict | None = None,
    options: dict | None = None,
    deadline_seconds: int = 180,
    think: bool | None = None,
) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "format": schema if schema is not None else IndexedProposal.model_json_schema(),
        "stream": True,
        "options": options if options is not None else OPTIONS,
        "keep_alive": "2m",
    }
    if think is not None:
        payload["think"] = think
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > 32000:
        raise InvalidProposalError("probe_context_budget")
    accumulated = []
    total_bytes = 0
    started = time.monotonic()
    with client.stream(
        "POST", "/api/generate", content=encoded, headers={"Content-Type": "application/json"}
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if time.monotonic() - started > deadline_seconds:
                raise InvalidProposalError("generation_deadline_unobserved")
            total_bytes += len(line.encode())
            if total_bytes > 524288:
                raise InvalidProposalError("model_output_budget")
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("model") != model:
                raise InvalidProposalError("response_model_mismatch")
            accumulated.append(chunk.get("response", ""))
            if chunk.get("done") is True:
                if chunk.get("done_reason") == "length":
                    raise InvalidProposalError("generation_incomplete")
                if chunk.get("done_reason") != "stop":
                    raise InvalidProposalError("generation_stop_unverified")
                chunk["response"] = "".join(accumulated)
                return chunk
    raise InvalidProposalError("generation_end_unobserved")


def run(model: str, suite: str, mode: str = "incremental") -> dict:
    if mode not in ("incremental", "replay"):
        raise InvalidProposalError("probe_mode_invalid")
    selected = cases()
    if suite == "reference":
        selected = [case for case in selected if case[1] == "reference"]
    report = {
        "schema": "indexed-task-events-local-v1",
        "synthetic": True,
        "utc": datetime.now(UTC).isoformat(),
        "model": model,
        "options": OPTIONS,
        "target": "local-loopback-not-TEST",
        "semantic_qualification": False,
        "phone_acceptance": False,
        "calendar_normalization": False,
        "server_context_coverage_verified": False,
        "suite": suite,
        "mode": mode,
        "source_hashes": code_hashes(),
        "rows": [],
    }
    ledgers = {}
    failed = set()
    started_run = time.monotonic()
    with httpx.Client(base_url="http://127.0.0.1:11434", timeout=60, trust_env=False) as client:
        report["fingerprint_before"] = fingerprint(client, model)
        for case_id, scenario, text, oracle in selected:
            if time.monotonic() - started_run > 600:
                report["budget_exhausted"] = True
                break
            if mode == "incremental" and scenario in failed:
                report["rows"].append({"case": case_id, "status": "dependency_failed"})
                continue
            ledger = (
                CandidateLedger()
                if mode == "replay"
                else ledgers.setdefault(scenario, CandidateLedger())
            )
            source = Source.parse(text, session=scenario)
            request = IndexedRequest.build(source, ledger)
            prompt = build_indexed_prompt(request)
            row = {
                "case": case_id,
                "source_sha256": digest(text),
                "prompt_sha256": digest(prompt),
                "source_chars": len(text),
                "prompt_chars": len(prompt),
            }
            started = time.monotonic()
            try:
                output = generate(client, model, prompt)
                row.update(
                    {
                        "response_sha256": digest(output["response"]),
                        "prompt_tokens": output.get("prompt_eval_count"),
                        "output_tokens": output.get("eval_count"),
                    }
                )
                parsed = IndexedProposal.model_validate_json(output["response"])
                apply_indexed(
                    ledger,
                    request,
                    parsed,
                    source_sha256=request.source_sha256,
                    state_sha256=request.state_sha256,
                )
                row.update(compare(ledger, oracle))
                row.update({"status": "evaluated", "events": len(parsed.events)})
                if not row["candidate_relation_pass"]:
                    failed.add(scenario)
            except Exception as exc:  # noqa: BLE001 — never log raw response or exception text
                row.update({"status": "error", "error_class": type(exc).__name__})
                if isinstance(exc, InvalidProposalError):
                    row["reason_code"] = str(exc)
                failed.add(scenario)
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            row["within_five_second_target"] = row["elapsed_seconds"] <= 5
            report["rows"].append(row)
            progress(
                {
                    "case": case_id,
                    "status": row["status"],
                    "reason_code": row.get("reason_code"),
                    "elapsed_seconds": row["elapsed_seconds"],
                }
            )
            if row["status"] == "error" and row.get("reason_code") != "generation_incomplete":
                # Conservatively stop after any invalid/unobserved stream or proposal;
                # a closed client connection never proves server cancellation.
                report["stopped_after_error"] = True
                break
        try:
            report["fingerprint_after"] = fingerprint(client, model)
            running = client.get("/api/ps")
            running.raise_for_status()
            report["runtime_models_after"] = [
                {k: m.get(k) for k in ("name", "digest", "size_vram", "context_length")}
                for m in running.json().get("models", [])
            ]
        except Exception as exc:  # noqa: BLE001
            report["fingerprint_after"] = {"error_class": type(exc).__name__}
    report["source_hashes_after"] = code_hashes()
    report["code_stable"] = report["source_hashes"] == report["source_hashes_after"]
    report["fingerprint_stable"] = report["fingerprint_before"] == report["fingerprint_after"]
    report["local_relation_cases_passed"] = (
        len(report["rows"]) == len(selected)
        and all(row.get("candidate_relation_pass") for row in report["rows"])
        and report["code_stable"]
        and report["fingerprint_stable"]
    )
    report["candidate_gate_passed"] = report["local_relation_cases_passed"] and all(
        row.get("within_five_second_target") for row in report["rows"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=sorted(MODELS), required=True)
    parser.add_argument("--suite", choices=["reference", "all"], default="all")
    parser.add_argument("--mode", choices=["incremental", "replay"], default="incremental")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.model, args.suite, args.mode)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    progress(
        {
            "cases": len(report["rows"]),
            "local_relation_cases_passed": report["local_relation_cases_passed"],
            "candidate_gate_passed": report["candidate_gate_passed"],
        }
    )
    return 0 if report["candidate_gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
