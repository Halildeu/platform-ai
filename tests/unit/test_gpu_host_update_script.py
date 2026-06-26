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
        self._assert_ps51_safe_script(self._read_script("update.ps1"))

    def test_drift_guard_script_is_ps51_safe(self) -> None:
        script = self._read_script("drift-guard.ps1")

        self._assert_ps51_safe_script(script)
        self.assertIn('$behindRange = "HEAD..{0}" -f $originRef', script)


if __name__ == "__main__":
    unittest.main()
