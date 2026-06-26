"""Regression tests for the GPU-host deploy update script."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class GpuHostUpdateScriptTests(unittest.TestCase):
    def test_update_script_avoids_branch_range_interpolation(self) -> None:
        script = (ROOT / "deploy/gpu-host/update.ps1").read_text(encoding="utf-8")

        self.assertIn('$originRef = "origin/{0}" -f $Branch', script)
        self.assertIn('$unpushedRange = "{0}..HEAD" -f $originRef', script)
        self.assertNotIn("$Branch..HEAD", script)
        self.assertNotIn("origin/$Branch..HEAD", script)
        self.assertNotIn('"origin/$Branch"', script)


if __name__ == "__main__":
    unittest.main()
