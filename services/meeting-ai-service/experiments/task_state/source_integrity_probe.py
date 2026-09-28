"""Fixed CPU/loopback transport qualification, not a semantic model evaluation."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from experiments.task_state.indexed_probe import MODELS, fingerprint, progress
from experiments.task_state.prototype import digest

HERE = Path(__file__).resolve().parent
MODEL = "llama3.1:8b"
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"const": True}},
    "required": ["ok"],
    "additionalProperties": False,
}


async def run():
    report = {
        "utc": datetime.now(UTC).isoformat(),
        "schema": "local-source-integrity-v1",
        "synthetic": True,
        "semantic_qualification": False,
        "phone_acceptance": False,
        "code_sha256": digest(Path(__file__).read_text(encoding="utf-8")),
        "rows": [],
    }
    started = time.monotonic()
    with httpx.Client(base_url="http://127.0.0.1:11434", timeout=5, trust_env=False) as identity:
        report["fingerprint_before"] = fingerprint(identity, MODEL)
        if report["fingerprint_before"]["version"] != "0.34.4":
            raise RuntimeError("unqualified version")
        info = identity.post("/api/show", json={"model": MODEL}).json()
        report["format"] = info["details"]["format"]
        report["architecture"] = info["model_info"]["general.architecture"]
        assert report["format"] == "gguf" and report["architecture"] == "llama"
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:11434", timeout=60, trust_env=False
        ) as client:
            for case in ("warm_shift_on", "full_source", "input_overflow", "output_limit"):
                payload = {
                    "model": MODEL,
                    "prompt": "Return the JSON object requested by the schema.",
                    "format": SCHEMA,
                    "stream": False,
                    "truncate": False,
                    "shift": case == "warm_shift_on",
                    "keep_alive": "2m",
                    "options": {
                        "num_ctx": 512,
                        "num_predict": 1 if case == "output_limit" else 64,
                        "num_gpu": 0,
                        "temperature": 0,
                        "seed": 42,
                    },
                }
                if case == "input_overflow":
                    payload["prompt"] = "evidence " * 2000
                row = {"case": case, "payload_sha256": digest(json.dumps(payload, sort_keys=True))}
                try:
                    remaining = 180 - (time.monotonic() - started)
                    async with asyncio.timeout(min(60, max(0.001, remaining))):
                        response = await client.post("/api/generate", json=payload)
                    body = response.json()
                    row.update(
                        status=response.status_code,
                        done=body.get("done"),
                        done_reason=body.get("done_reason"),
                        prompt_tokens=body.get("prompt_eval_count"),
                        output_tokens=body.get("eval_count"),
                        body_sha256=digest(response.text),
                    )
                    if case == "input_overflow":
                        row["passed"] = (
                            response.status_code == 400
                            and "context" in body.get("error", "").lower()
                        )
                    else:
                        row["passed"] = (
                            response.status_code == 200
                            and body.get("model") == MODEL
                            and body.get("done") is True
                            and body.get("done_reason")
                            == ("length" if case == "output_limit" else "stop")
                        )
                    runtime = identity.get("/api/ps")
                    runtime.raise_for_status()
                    loaded = [r for r in runtime.json()["models"] if r.get("name") == MODEL]
                    row["cpu_runtime_verified"] = (
                        len(loaded) == 1
                        and loaded[0].get("digest") == MODELS[MODEL]
                        and loaded[0].get("size_vram") == 0
                        and loaded[0].get("context_length") == 512
                    )
                    row["passed"] = row["passed"] and row["cpu_runtime_verified"]
                except Exception as exc:  # noqa: BLE001 -- metadata only, no content
                    row.update(passed=False, error_class=type(exc).__name__)
                report["rows"].append(row)
                progress(row)
                if not row["passed"]:
                    break
        report["fingerprint_after"] = fingerprint(identity, MODEL)
    report["passed"] = (
        len(report["rows"]) == 4
        and all(r["passed"] for r in report["rows"])
        and report["fingerprint_before"] == report["fingerprint_after"]
        and report["code_sha256"] == digest(Path(__file__).read_text(encoding="utf-8"))
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


if __name__ == "__main__":
    path = HERE / "local-source-integrity-r2-20260928.json"
    if path.exists():
        raise SystemExit("report already exists")
    report = asyncio.run(run())
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if report["passed"] else 1)
