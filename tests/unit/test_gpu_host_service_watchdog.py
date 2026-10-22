"""gitops#3888 — GPU-host service watchdog static contract.

Behaviour was verified live on SRB-AIDENETIMPC (2026-10-22): real tasks up ->
exit 0; deploy mutex present -> skip; dummy task down -> probe exit 3, start
via schtasks /Run; disabled (fenced) -> untouched. These tests pin the
safety properties so a refactor cannot silently drop them.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

GPU = Path(__file__).resolve().parents[2] / "deploy" / "gpu-host"


def read(name: str) -> str:
    return (GPU / name).read_text(encoding="utf-8")


class TestServiceWatchdogContract(unittest.TestCase):
    def setUp(self) -> None:
        self.wd = read("service-watchdog.ps1")

    def test_never_acquires_the_deploy_mutex(self) -> None:
        self.assertIn('"Global\\platform-ai-gpu-deploy-v1"', self.wd)
        self.assertIn("TryOpenExisting", self.wd)
        self.assertNotIn("WaitOne", self.wd)
        # the deploy check runs before any start attempt
        self.assertLess(self.wd.index("if (Test-GpuHostDeployInProgress)"),
                        self.wd.index("foreach ($svc in $script:ManagedServices)"))

    def test_mutex_name_matches_update_ps1(self) -> None:
        self.assertIn('"Global\\platform-ai-gpu-deploy-v1"', read("update.ps1"))

    def test_fenced_tasks_are_left_alone(self) -> None:
        enabled = self.wd.index("if (-not $state.Enabled)")
        start = self.wd.index("Invoke-SchtasksRun -TaskName $TaskName")
        self.assertLess(enabled, start)

    def test_starts_only_when_no_instance_and_no_listener(self) -> None:
        self.assertRegex(self.wd, r"\$state\.Running -or \(\$owners\.Succeeded -and @\(\$owners\.Owners\)\.Count -gt 0\)")
        self.assertIn("& schtasks.exe /Run /TN $TaskName", self.wd)

    def test_manages_exactly_live_stt_and_meeting_ai_ports(self) -> None:
        self.assertIn('TaskName = "platform-ai-live-stt"; Port = 8200', self.wd)
        self.assertIn('TaskName = "platform-ai-meeting-ai"; Port = 8300', self.wd)

    def test_probe_only_never_starts(self) -> None:
        probe = self.wd.index("if ($ProbeOnly)")
        self.assertLess(probe, self.wd.index("$exit = Invoke-SchtasksRun"))

    def test_no_invalid_drive_qualified_interpolation(self) -> None:
        # "$TaskName:" inside a string is a PowerShell parse error.
        self.assertIsNone(re.search(r'"[^"]*\$TaskName:', self.wd))


class TestWatchdogRegistration(unittest.TestCase):
    def test_task_is_non_overlapping_bounded_and_repeating(self) -> None:
        reg = read("register-service-watchdog.ps1")
        self.assertIn('-TaskName "platform-ai-service-watchdog"', reg)
        self.assertIn("-MultipleInstances IgnoreNew", reg)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Minutes 10)", reg)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)", reg)
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", reg)
        self.assertIn('-UserId "SYSTEM"', reg)

    def test_install_registers_watchdog_only_after_acceptance(self) -> None:
        install = read("install.ps1")
        self.assertLess(install.index("$acceptanceExit = Invoke-GpuHostControllerUpdate"),
                        install.index('"register-service-watchdog.ps1"'))


if __name__ == "__main__":
    unittest.main()
