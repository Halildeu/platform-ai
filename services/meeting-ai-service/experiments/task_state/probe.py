"""Fixed synthetic LOCAL model experiment; never TEST/phone qualification.

No arbitrary transcript/endpoint/model arguments. Does not pull or install models.
Only metadata leaves this process. Candidate prose is neither logged nor exported.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from experiments.task_state.prototype import (
    CandidateLedger,
    InvalidProposalError,
    Proposal,
    Source,
    build_prompt,
    digest,
)

MODEL = "llama3.1:8b"
DIGEST = "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e"
OPTIONS = {"num_ctx": 8192, "temperature": 0.0, "top_p": 0.9, "num_predict": 4096}
HERE = Path(__file__).resolve().parent


def fingerprint(client: httpx.Client) -> dict:
    version = client.get("/api/version")
    version.raise_for_status()
    tags = client.get("/api/tags")
    tags.raise_for_status()
    models = [m for m in tags.json()["models"] if m["name"] == MODEL]
    if len(models) != 1 or models[0]["digest"] != DIGEST:
        raise InvalidProposalError("model_fingerprint_mismatch")
    return {"version": version.json()["version"], "model": MODEL, "digest": DIGEST}


def generate(client: httpx.Client, prompt: str) -> dict:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "format": Proposal.model_json_schema(),
        "stream": False,
        "options": OPTIONS,
    }
    # Bounded synthetic input. Byte size is reported, NOT treated as a tokenizer
    # guarantee. Actual token counters cannot prove absence of server truncation.
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > 18000:
        raise InvalidProposalError("probe_context_budget")
    body = bytearray()
    with client.stream(
        "POST", "/api/generate", content=encoded, headers={"Content-Type": "application/json"}
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 262144:
                raise InvalidProposalError("model_output_budget")
    result = json.loads(body)
    if result.get("done") is not True or result.get("done_reason") == "length":
        raise InvalidProposalError("generation_incomplete")
    if result.get("model") != MODEL:
        raise InvalidProposalError("response_model_mismatch")
    return result


def expected(work, owner, *, status="active", date=None, hour=None):
    return {"work": work, "owner": owner, "status": status, "date": date, "hour": hour}


def cases():
    reference = json.loads((HERE / "reference.json").read_text(encoding="utf-8"))
    initial = [
        expected(["sunum", "dosya"], "Zeynep", date="28 Eylül 2026", hour="10"),
        expected(["bütçe"], "Mehmet", date="aynı gün", hour="12"),
        expected(["müşteri", "liste"], "Ayşe Yılmaz"),
        expected(["toplantı", "rapor"], "Sevil Karakaş"),
        expected(["teklif", "dosya"], "Halil Koçoğlu"),
        expected(["toplantı", "davet"], "Deniz Arslan"),
        expected(["ürün", "görsel"], "Elif Demir"),
        expected(["sunum", "bağlantı"], "Can Kaya"),
    ]
    final = [dict(item) for item in initial]
    final[0]["hour"] = "11"
    final[6]["owner"] = "Ayşe Yılmaz"
    final[7]["status"] = "cancelled"
    text = reference["initial"]
    changed = text + " " + reference["change"].replace("10 değil, 11", "10 değil. 11")
    return [
        ("reference-initial", "reference", text, initial),
        ("reference-change-split", "reference", changed, final),
        ("reference-unchanged", "reference", changed + " Ekip başka konuları görüştü.", final),
        (
            "address-first-person",
            "address",
            "Mehmet. Bütçe tablosunu ben kontrol edeceğim.",
            [expected(["bütçe", "tablo"], None)],
        ),
        (
            "refused-cancel-initial",
            "refused",
            "Derya raporu hazırlayacak.",
            [expected(["rapor"], "Derya")],
        ),
        (
            "refused-cancel-change",
            "refused",
            "Derya raporu hazırlayacak. Rapor görevini iptal edelim mi? Hayır, iptal etmiyoruz.",
            [expected(["rapor"], "Derya")],
        ),
        (
            "same-owner-distinct-work",
            "distinct",
            "Ece raporu hazırlayacak. Ece bütçeyi kontrol edecek.",
            [expected(["rapor"], "Ece"), expected(["bütçe"], "Ece")],
        ),
        (
            "historical-not-current",
            "historical",
            "Geçen yılki tutanakta 'Derya raporu hazırlayacak' yazıyordu. "
            "Bugün yeni görev vermedik.",
            [],
        ),
        (
            "twelve-tasks",
            "twelve",
            " ".join(f"Ekip rapor {i} hazırlayacak." for i in range(12)),
            [expected([f"rapor {i}"], "Ekip") for i in range(12)],
        ),
    ]


def compare(ledger: CandidateLedger, oracle: list[dict]) -> dict:
    remaining = list(ledger.tasks.values())
    matched = 0
    missing = 0
    metadata_errors = 0
    field_errors = {key: 0 for key in ("owner", "status", "date", "time")}
    for wanted in oracle:
        candidates = [
            t
            for t in remaining
            if all(
                re.search(
                    re.escape(token) + (r"\b" if token[-1].isdigit() else ""),
                    t.description.text.lower(),
                )
                for token in wanted["work"]
            )
        ]
        if len(candidates) != 1:
            missing += 1
            continue
        task = candidates[0]
        remaining.remove(task)
        actual_owner = task.owner.text if task.owner else None
        date = task.date.text if task.date else None
        hour = re.findall(r"\d+", task.time.text) if task.time else []
        checks = {
            "owner": actual_owner == wanted["owner"],
            "status": task.status == wanted["status"],
            "date": (
                (wanted["date"] in date) if date and wanted["date"] else date == wanted["date"]
            ),
            "time": hour == ([wanted["hour"]] if wanted["hour"] else []),
        }
        for key, passed in checks.items():
            field_errors[key] += int(not passed)
        correct = all(checks.values())
        matched += int(correct)
        metadata_errors += int(not correct)
    return {
        "expected_tasks_including_tombstones": len(oracle),
        "candidate_tasks": len(ledger.tasks),
        "matched_tasks": matched,
        "missing_or_ambiguous_work": missing,
        "unexpected_tasks": len(remaining),
        "metadata_or_status_errors": metadata_errors,
        "field_mismatches": field_errors,
        "unresolved_units": len(ledger.unresolved),
        "candidate_relation_pass": matched == len(oracle)
        and not remaining
        and not ledger.unresolved,
    }


def code_hashes() -> dict:
    return {
        p.name: digest(p.read_text(encoding="utf-8"))
        for p in (HERE / "prototype.py", HERE / "probe.py", HERE / "reference.json")
    }


def run() -> dict:
    report = {
        "schema": "candidate-task-events-probe-v1",
        "synthetic": True,
        "utc": datetime.now(UTC).isoformat(),
        "model": MODEL,
        "expected_digest": DIGEST,
        "options": OPTIONS,
        "target": "local-loopback-experiment-not-TEST",
        "semantic_qualification": False,
        "phone_acceptance": False,
        "calendar_normalization": False,
        "server_context_coverage_verified": False,
        "source_hashes": code_hashes(),
        "rows": [],
    }
    ledgers = {}
    failed_scenarios = set()
    overall_started = time.monotonic()
    selected_cases = cases()
    with httpx.Client(base_url="http://127.0.0.1:11434", timeout=60, trust_env=False) as client:
        report["fingerprint_before"] = fingerprint(client)
        for case_id, scenario, text, oracle in selected_cases:
            if time.monotonic() - overall_started > 420:
                report["budget_exhausted"] = True
                break
            if scenario in failed_scenarios:
                report["rows"].append({"case": case_id, "status": "dependency_failed"})
                continue
            ledger = ledgers.setdefault(scenario, CandidateLedger())
            source = Source.parse(text, session=scenario)
            prompt = build_prompt(source, ledger)
            row = {
                "case": case_id,
                "source_sha256": digest(text),
                "prompt_sha256": digest(prompt),
                "source_chars": len(text),
            }
            started = time.monotonic()
            try:
                generated = generate(client, prompt)
                proposal = Proposal.model_validate_json(generated["response"])
                ledger.apply(source, proposal)
                row.update(compare(ledger, oracle))
                row.update(
                    {
                        "status": "evaluated",
                        "event_count": len(proposal.events),
                        "operations": {
                            op: sum(e.op == op for e in proposal.events)
                            for op in (
                                "create",
                                "reassign",
                                "reschedule",
                                "cancel",
                                "complete",
                                "reopen",
                            )
                        },
                        "prompt_tokens": generated.get("prompt_eval_count"),
                        "output_tokens": generated.get("eval_count"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — metadata only; no source or model output
                row.update({"status": "error", "error_class": type(exc).__name__})
                if isinstance(exc, InvalidProposalError):
                    row["reason_code"] = str(exc)
                failed_scenarios.add(scenario)
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            row["within_five_second_target"] = row["elapsed_seconds"] <= 5
            report["rows"].append(row)
            if row.get("error_class") in ("ReadTimeout", "ConnectTimeout"):
                # A client timeout does not prove model work stopped. Do not queue
                # more inference on the shared local server after losing observation.
                report["stopped_after_transport_timeout"] = True
                break
        try:
            report["fingerprint_after"] = fingerprint(client)
        except Exception as exc:  # noqa: BLE001 — preserve metadata, never raw response
            report["fingerprint_after"] = {"error_class": type(exc).__name__}
    report["source_hashes_after"] = code_hashes()
    report["code_stable"] = report["source_hashes"] == report["source_hashes_after"]
    report["fingerprint_stable"] = report["fingerprint_before"] == report["fingerprint_after"]
    report["candidate_gate_passed"] = (
        len(report["rows"]) == len(selected_cases)
        and all(
            r.get("candidate_relation_pass") and r.get("within_five_second_target")
            for r in report["rows"]
        )
        and report["fingerprint_stable"]
        and report["code_stable"]
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    # stdout intentionally contains only aggregate metadata.
    sys.stdout.write(
        json.dumps(
            {"cases": len(report["rows"]), "candidate_gate_passed": report["candidate_gate_passed"]}
        )
        + "\n"
    )
    return 0 if report["candidate_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
