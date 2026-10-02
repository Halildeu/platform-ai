"""Local-only experiment runner. Blocks Python network I/O and subprocesses.

This is a process-level guard for the selected Python tests, not an OS sandbox.
No HTTP service, real model, provider or deployment is started.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("baseline", "candidate", "regression"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Tests must not inherit operational configuration or credentials.
    keep = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC", "SYSTEMDRIVE"}
    for key in list(os.environ):
        if key.upper() not in keep:
            del os.environ[key]
    os.environ.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        MAI_BACKEND="mock",
        MAI_APP_ENV="dev",
    )
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    denied: list[str] = []

    def audit(event: str, unused_args: tuple[object, ...]) -> None:
        if event in {
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.sendto",
            "subprocess.Popen",
            "os.system",
        }:
            denied.append(event)
            raise RuntimeError("offline-experiment-blocked-external-io")

    sys.addaudithook(audit)
    try:
        socket.getaddrinfo("offline.invalid", 443)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("offline-guard-self-check-failed")
    self_check = list(denied)
    denied.clear()
    import pytest

    paths = [str(EXPERIMENT / "tests" / "test_baseline.py")]
    if args.stage != "baseline":
        paths.append(str(EXPERIMENT / "tests" / "test_option_catalog.py"))
        paths.append(str(EXPERIMENT / "tests" / "test_punctuation_projection.py"))
    if args.stage != "baseline":
        paths.append(str(EXPERIMENT / "tests" / "test_candidate.py"))
        paths.append(str(EXPERIMENT / "tests" / "test_local_compare.py"))
    if args.stage == "regression":
        paths.extend(
            str(ROOT / "tests/unit" / name)
            for name in (
                "test_extractive_selection.py",
                "test_citation.py",
                "test_citation_entailment.py",
                "test_live_context.py",
                "test_ollama_analyzer.py",
                "test_mock_analyzer.py",
            )
        )
    flags = ["-q", "-o", "addopts=", "-p", "pytest_cov"]
    excluded = []
    if args.stage == "regression" and sys.platform == "win32":
        # Windows asyncio creates a loopback socketpair even for ASGI MockTransport.
        # Keep the no-socket guard intact and record this unexecuted endpoint test.
        excluded = [
            "tests/unit/test_live_context.py::"
            "test_live_endpoint_threads_cursor_and_keeps_original_citation_offsets"
        ]
        for node in excluded:
            flags.extend(["--deselect", node])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    flags.append(f'--junitxml={args.output.with_suffix(".junit.xml")}')
    if args.stage != "baseline":
        flags += [
            "--cov=experiments.owner_context.candidate",
            "--cov=experiments.owner_context.option_catalog",
            "--cov=experiments.owner_context.punctuation_projection",
            "--cov-branch",
            "--cov-report=term-missing",
            f'--cov-report=json:{args.output.with_suffix(".coverage.json")}',
        ]
    exit_code = pytest.main(paths + flags)
    receipt = {
        "stage": args.stage,
        "pytestExit": int(exit_code),
        "guardSelfCheck": self_check,
        "blockedIoDuringTests": denied,
        "modelInvoked": False,
        "deployed": False,
        "excludedTests": excluded,
        "exclusionReason": (
            "Windows asyncio socketpair conflicts with strict no-socket guard" if excluded else None
        ),
        "scope": "offline synthetic controlled-response experiment; no live quality claim",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(receipt) + "\n")
    return int(exit_code) or (2 if denied else 0)


if __name__ == "__main__":
    raise SystemExit(main())
