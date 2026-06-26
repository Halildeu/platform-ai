"""Regression tests for the GPU-host deploy update script."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class GpuHostUpdateScriptTests(unittest.TestCase):
    def _read_script(self, name: str) -> str:
        return (ROOT / "deploy/gpu-host" / name).read_text(encoding="utf-8")

    def _assert_ps51_safe_script(self, script: str) -> None:
        script.encode("ascii")
        self.assertIn('$originRef = "origin/{0}" -f $Branch', script)
        self.assertIn('$unpushedRange = "{0}..HEAD" -f $originRef', script)
        self.assertIn('[string]$RepoRoot = ""', script)
        self.assertNotIn("Split-Path -Parent (Split-Path -Parent $PSScriptRoot)", script)
        self.assertNotIn("$Branch..HEAD", script)
        self.assertNotIn("origin/$Branch..HEAD", script)
        self.assertNotIn('"origin/$Branch"', script)

    def test_update_script_is_ps51_safe(self) -> None:
        script = self._read_script("update.ps1")

        self._assert_ps51_safe_script(script)
        self.assertIn("function Invoke-GitStream", script)
        self.assertNotIn("2>&1 | Out-Host", script)

    def test_drift_guard_script_is_ps51_safe(self) -> None:
        script = self._read_script("drift-guard.ps1")

        self._assert_ps51_safe_script(script)
        self.assertIn('$behindRange = "HEAD..{0}" -f $originRef', script)

    def test_live_stt_start_sets_cold_load_timeout_before_local_overrides(self) -> None:
        script = self._read_script("start-live-stt.ps1")

        timeout_line = '$env:STT_REQUEST_TIMEOUT = "180"'
        self.assertIn(timeout_line, script)
        self.assertLess(script.index(timeout_line), script.index("$envLocal = Join-Path"))

    def test_live_stt_update_warmup_budget_matches_gpu_cold_load_timeout(self) -> None:
        script = self._read_script("update.ps1")

        self.assertIn("--max-time 240", script)
        self.assertNotIn("--max-time 120 -F", script)


if __name__ == "__main__":
    unittest.main()
