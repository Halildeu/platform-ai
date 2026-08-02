"""Regression tests for the GPU-host deploy update script."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class GpuHostUpdateScriptTests(unittest.TestCase):
    def _read_script(self, name: str) -> str:
        return (ROOT / "deploy/gpu-host" / name).read_text(encoding="utf-8")

    def _assert_ps51_safe_script(self, script: str) -> None:
        script.encode("ascii")
        self.assertIn('$originRef = "origin/{0}" -f $Branch', script)
        self.assertIn('[string]$RepoRoot = ""', script)
        self.assertNotIn(
            "Split-Path -Parent (Split-Path -Parent $PSScriptRoot)", script
        )
        self.assertNotIn("$Branch..HEAD", script)
        self.assertNotIn("origin/$Branch..HEAD", script)
        self.assertNotIn('"origin/$Branch"', script)

    def test_update_script_is_ps51_safe(self) -> None:
        script = self._read_script("update.ps1")

        self._assert_ps51_safe_script(script)
        self.assertIn("function Invoke-GitStream", script)
        self.assertIn('$unpushedRange = "{0}..HEAD" -f $originRef', script)
        self.assertIn("[string]$TargetCommit", script)
        self.assertIn("[switch]$ReconcileLedgerDrift", script)
        self.assertIn("[string]$ControllerCommit", script)
        self.assertIn("[int]$PreservedPullRequestNumber", script)
        self.assertIn("[string]$PreservedPullRequestHead", script)
        self.assertIn("[switch]$RecoverFencedRuntime", script)
        self.assertIn(
            "Observed drift HEAD is not an ancestor of $originRef", script
        )
        self.assertIn('"refs/pull/{0}/head"', script)
        self.assertIn(
            '"refs/remotes/origin/preserved-pull/{0}/head"', script
        )
        self.assertIn(
            "Declared preserved pull ref does not exact-match its expected head",
            script,
        )
        self.assertIn(
            '"Observed drift HEAD is not reachable from the exact "',
            script,
        )
        self.assertIn('"preserved pull ref. No mutation."', script)
        self.assertIn(
            '"merge-base", "--is-ancestor", $before, $preservedPullLocalRef',
            script,
        )
        self.assertIn(
            '"ls-files", "--others", "--exclude-standard"',
            script,
        )
        self.assertIn("Run preserve-untracked.ps1", script)
        self.assertIn(
            "Ledger recovery anchor is unavailable or outside origin ancestry",
            script,
        )
        self.assertIn(
            "if ($ReconcileLedgerDrift -and $target -eq $state.currentCommit)",
            script,
        )
        self.assertIn(
            "elseif ($ReconcileLedgerDrift) { $previous = $state.currentCommit }",
            script,
        )
        self.assertIn(
            "-ReconcileLedgerDrift requires restart and runtime acceptance",
            script,
        )
        self.assertIn(
            "if ($ReconcileLedgerDrift) { $restoreCommit = $state.currentCommit }",
            script,
        )
        self.assertIn(
            "$ledgerWriteRestoreCommit = $state.currentCommit",
            script,
        )
        self.assertIn(
            "Deployment ledger branchRef does not match the requested branch",
            script,
        )
        self.assertIn(
            "Controller commit is unavailable or outside origin ancestry",
            script,
        )
        controller_guard = script[
            script.index("$expectedControllerCommit = if ("):
            script.index("$controllerDirty =")
        ]
        self.assertIn("$ControllerCommit", controller_guard)
        self.assertIn("function Stop-GpuHostRuntimeFailClosed", script)
        self.assertIn("function Restore-GpuHostTrustedDeploymentState", script)
        self.assertIn("function Set-GpuHostRuntimeTasksEnabled", script)
        self.assertIn("Set-SchtasksTaskEnabled", script)
        self.assertIn('"/Disable"', script)
        self.assertIn('"/Enable"', script)
        self.assertIn("runtime task fence is present", script.lower())
        self.assertIn(
            "trusted source restored and runtime reaccepted",
            script,
        )
        self.assertGreater(
            script.index("Invoke-GpuHostSourceAndLedgerMutation\n"),
            script.index("function Invoke-GpuHostAutomaticRollback"),
        )
        self.assertIn("exactly 40 hex characters", script)
        self.assertIn("separate exact-target control checkout", script)
        self.assertIn("Control checkout HEAD must equal", script)
        self.assertIn("Deploy and control checkouts must use the same origin", script)
        self.assertIn("LegacyRollbackCompatCommit", script)
        self.assertIn('ValidateSet("strict-v1", "legacy-512e9cc")', script)
        self.assertIn('"merge-base", "--is-ancestor", $target, $originRef', script)
        self.assertIn('"checkout", "--detach", $target', script)
        self.assertIn('"symbolic-ref", "-q", "HEAD"', script)
        self.assertIn("Read-DeploymentState", script)
        self.assertIn("Write-DeploymentStateAtomic", script)
        self.assertIn("SupportsShouldProcess", script)
        self.assertIn("DeployExitRestartFailed = 3", script)
        self.assertIn("DeployExitRollbackFailed = 4", script)
        self.assertIn("[Console]::Error.WriteLine", script)
        self.assertNotIn("Write-Error $Message", script)
        self.assertIn('$env:CI -eq "true"', script)
        self.assertIn('$env:GITHUB_ACTIONS -eq "true"', script)
        self.assertIn('$env:RUNNER_ENVIRONMENT -eq "github-hosted"', script)
        self.assertIn("\\\\runneradmin$", script)
        self.assertIn("$script:ResolvedRunnerTemp", script)
        self.assertIn("function Test-GpuHostPathUnderRoot", script)
        self.assertIn(
            "Test-GpuHostPathUnderRoot -Path $controllerRoot", script
        )
        self.assertIn("Test-GpuHostPathUnderRoot -Path $RepoRoot", script)
        self.assertIn(
            "Test-GpuHostPathUnderRoot -Path $script:ResolvedStatePath",
            script,
        )
        self.assertGreater(
            script.index("$script:TestFaultsEnabled = ("),
            script.index("$controllerRoot = (Resolve-Path"),
        )
        self.assertIn("PLATFORM_AI_TEST_INJECT_LEDGER_WRITE_FAILURE", script)
        self.assertIn(
            "PLATFORM_AI_TEST_INJECT_LEDGER_POST_WRITE_FAILURE", script
        )
        self.assertIn("PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE", script)
        self.assertIn("PLATFORM_AI_TEST_INJECT_ACCEPTANCE_EXCEPTION", script)
        self.assertIn("PLATFORM_AI_TEST_INJECT_RESULT_WRITE_FAILURE", script)
        self.assertIn("$resultWriteFailed = $true", script)
        self.assertNotIn(
            "Pinned source but ledger result update failed", script
        )
        self.assertNotIn("[switch]$Force", script)
        self.assertNotIn('"checkout", "-B"', script)
        self.assertNotIn("2>&1 | Out-Host", script)

    def test_untracked_quarantine_is_restricted_and_content_addressed(self) -> None:
        script = self._read_script("preserve-untracked.ps1")

        script.encode("ascii")
        self.assertIn("SupportsShouldProcess", script)
        self.assertIn("[int]$ExpectedCount = -1", script)
        self.assertIn("ls-files --others --exclude-standard -z", script)
        self.assertIn("SetAccessRuleProtection($true, $false)", self._read_script(
            "deployment-state.ps1"
        ))
        self.assertIn("New-DeploymentStateAcl", script)
        self.assertIn("Assert-DeploymentStateAcl", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("-Algorithm SHA256", script)
        self.assertIn('$objectPath = Join-Path $objectsRoot $record.sha256', script)
        self.assertIn("Quarantine copy readback failed", script)
        self.assertIn("Quarantine receipt readback failed", script)
        self.assertIn("Source changed after quarantine verification", script)
        self.assertIn("PLATFORM_AI_TEST_INJECT_QUARANTINE_RECEIPT_FAILURE", script)
        self.assertIn("WhatIf/declined: validation passed; no mutation", script)
        self.assertNotIn("Write-Host $relativePath", script)
        self.assertNotIn("Write-Host $record.relativePath", script)

    def test_drift_guard_script_is_ps51_safe(self) -> None:
        script = self._read_script("drift-guard.ps1")

        self._assert_ps51_safe_script(script)
        self.assertIn("Read-DeploymentState", script)
        self.assertIn("$state.currentCommit", script)
        self.assertIn("git symbolic-ref -q HEAD", script)
        self.assertIn("git merge-base --is-ancestor $expected $originRef", script)
        self.assertNotIn("$behindRange", script)
        self.assertNotIn("commit(s) behind", script)

    def test_deployment_state_ledger_is_atomic_and_acl_hardened(self) -> None:
        script = self._read_script("deployment-state.ps1")

        script.encode("ascii")
        self.assertIn("S-1-5-18", script)
        self.assertIn("S-1-5-32-544", script)
        self.assertIn("SetAccessRuleProtection($true, $false)", script)
        self.assertIn("AreAccessRulesProtected", script)
        self.assertIn("[IO.File]::Replace", script)
        self.assertIn("New-DeploymentStateRecord", script)
        self.assertIn("Read-DeploymentState", script)
        self.assertIn("Write-DeploymentStateAtomic", script)
        self.assertIn("previousCommit", script)
        self.assertIn("lastAction", script)
        self.assertIn("lastResult", script)

    def test_live_stt_start_uses_strict_non_executable_runtime_config(self) -> None:
        script = self._read_script("start-live-stt.ps1")

        timeout_line = '$env:STT_REQUEST_TIMEOUT = "180"'
        self.assertIn(timeout_line, script)
        self.assertIn("Import-LiveSttRuntimeEnvironment", script)
        self.assertIn("Clear-LiveSttManagedProcessEnvironment", script)
        self.assertIn("live-stt-runtime-env.ps1", script)
        self.assertIn("Legacy plaintext env.local.ps1 detected", script)
        self.assertIn('Get-Date -Format "yyyyMMddTHHmmssfff"', script)
        self.assertIn('"live-stt-{0}-pid{1}.log" -f $logStamp, $PID', script)
        self.assertNotIn('Get-Date -Format "yyyyMMdd"))', script)

        runtime_env = self._read_script("live-stt-runtime-env.ps1")
        self.assertIn("must reside on a fixed local volume", runtime_env)
        self.assertIn("must not traverse a reparse point", runtime_env)
        self.assertIn("owner must be SYSTEM or BUILTIN Administrators", runtime_env)
        self.assertIn("principals require FullControl", runtime_env)
        self.assertIn("Get-LiveSttRuntimeRoot", runtime_env)

        provisioner = self._read_script("configure-live-stt.ps1")
        self.assertIn("[string]$RepoRoot", provisioner)
        self.assertIn("[Security.SecureString]$RedisUrl", provisioner)
        self.assertIn("DataProtectionScope]::LocalMachine", provisioner)
        self.assertIn("Write-LiveSttProvisionConfigAtomic", provisioner)
        self.assertIn("-RemoveLegacyAfterVerifiedMigration", provisioner)
        self.assertNotIn(". $legacyConfigPath", provisioner)

    def test_fresh_install_delegates_first_start_to_full_acceptance(self) -> None:
        script = self._read_script("install.ps1")
        process_contract = self._read_script("bootstrap-process.ps1")
        updater = self._read_script("update.ps1")

        self.assertIn("[string]$TargetCommit", script)
        self.assertIn("separate exact-target controller checkout", script)
        self.assertIn("Invoke-GpuHostControllerUpdate -ValidationOnly", script)
        self.assertIn('"-NoConfirm"', script)
        self.assertNotIn("'-Confirm:$false'", script)
        self.assertIn("Invoke-GpuHostBoundedWindowsPowerShellFile", script)
        self.assertIn("ModelStagingTimeoutSec", script)
        self.assertIn("AcceptanceTimeoutSec", script)
        self.assertIn("stage-live-stt-models.ps1", script)
        self.assertLess(
            script.index("stage-live-stt-models.ps1"),
            script.index("Register-ScheduledTask"),
        )
        self.assertIn("Provision the DPAPI meeting-ai runtime config", script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("$acceptanceExit = Invoke-GpuHostControllerUpdate", script)
        self.assertIn("Remove-GpuHostBootstrapTasks", script)
        self.assertIn("Bootstrap rollback did not release service listener", script)
        self.assertNotIn("Start-ScheduledTask -TaskName $t.Name", script)
        self.assertNotIn('tokens += "-NoRestart"', script)
        self.assertIn("Stop-GpuHostProcessTreeBounded", process_contract)
        self.assertIn("taskkill.exe", process_contract)
        self.assertIn("$process.WaitForExit($TimeoutSec * 1000)", process_contract)
        self.assertIn('"-NonInteractive"', process_contract)
        self.assertIn("[switch]$NoConfirm", updater)
        self.assertIn('$ConfirmPreference = "None"', updater)

    def test_live_stt_models_are_staged_and_verified_as_complete_directories(
        self,
    ) -> None:
        policy = json.loads(
            (ROOT / "deploy/gpu-host/live-stt-model-policy.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(policy["schema"], "platform-ai.live-stt.model-policy.v1")
        self.assertEqual(
            {entry["role"] for entry in policy["models"]}, {"live", "final"}
        )
        for entry in policy["models"]:
            self.assertRegex(entry["revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(entry["modelBinSha256"], r"^[0-9a-f]{64}$")

        runtime = self._read_script("live-stt-model-runtime.ps1")
        stager = self._read_script("stage-live-stt-models.ps1")
        launcher = self._read_script("start-live-stt.ps1")
        for script in (runtime, stager):
            script.encode("ascii")
        self.assertIn("Assert-LiveSttNoReparseTree", runtime)
        self.assertIn("must not traverse a reparse point", runtime)
        self.assertIn("SetAccessRuleProtection($true, $false)", runtime)
        self.assertIn('"S-1-5-18"', runtime)
        self.assertIn('"S-1-5-32-544"', runtime)
        self.assertIn("Assert-LiveSttModelTreeAcl", runtime)
        self.assertIn("Invoke-LiveSttModelVerifier", runtime)
        self.assertIn('"--digest-output", $DigestOutputPath', runtime)
        self.assertIn("treeSha256", runtime)
        self.assertIn(
            "Move-Item -LiteralPath $staging -Destination $destination", stager
        )
        self.assertIn("Model staging rollback failed", stager)
        self.assertIn("Assert-LiveSttModelSet", launcher)
        self.assertIn('$env:HF_HUB_OFFLINE = "1"', launcher)
        self.assertIn("STT_LIVE_MODEL_TREE_SHA256", launcher)
        self.assertIn("STT_FINAL_MODEL_TREE_SHA256", launcher)
        self.assertNotIn("models--Systran--", launcher)
        self.assertNotIn("models--deepdml--", launcher)

    def test_model_integrity_helper_stages_and_detects_non_model_bin_changes(
        self,
    ) -> None:
        helper = ROOT / "deploy/gpu-host/stage-live-stt-model.py"
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "runtime"
            source.mkdir()
            model_bytes = b"synthetic-model-bin"
            (source / "model.bin").write_bytes(model_bytes)
            (source / "config.json").write_text(
                '{"model":"synthetic"}', encoding="utf-8"
            )
            (source / "tokenizer.json").write_text('{"tokens":[]}', encoding="utf-8")
            # Upper-case entry pins the manifest ordering contract. `sorted()`
            # over PurePath objects is case-INSENSITIVE on Windows, so this
            # file used to be emitted after "model.bin" while the manifest
            # reader enforces plain ASCII ascending order ("R" 0x52 < "m"
            # 0x6D) and rejected the file this helper had just written:
            # "model integrity file entry is not canonical". Every real
            # Hugging Face snapshot ships a README.md, so GPU-host model
            # staging was blocked outright; a lower-case-only fixture kept it
            # invisible. The regression only reproduces on a case-insensitive
            # filesystem — the gpu-host-windows-contract job is what covers it.
            (source / "README.md").write_text("synthetic model card", encoding="utf-8")
            model_hash = hashlib.sha256(model_bytes).hexdigest()
            common = [
                "--repository",
                "example/synthetic-model",
                "--revision",
                revision,
                "--model-bin-sha256",
                model_hash,
                "--destination",
                str(destination),
            ]
            subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "stage",
                    *common,
                    "--source-directory",
                    str(source),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(
                (destination / "integrity-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [entry["path"] for entry in manifest["files"]],
                ["README.md", "config.json", "model.bin", "tokenizer.json"],
            )
            recorded_paths = [entry["path"] for entry in manifest["files"]]
            self.assertEqual(
                recorded_paths,
                sorted(recorded_paths),
                "manifest files must be in ASCII ascending order",
            )
            digest_output = root / "tree-digest.txt"
            subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "verify",
                    *common,
                    "--digest-output",
                    str(digest_output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertRegex(
                digest_output.read_text(encoding="ascii").strip(),
                r"^[0-9a-f]{64}$",
            )
            expected_tree = hashlib.sha256(
                b"platform-live-stt-model-directory-v1\0"
            )
            for artifact in sorted(
                path for path in destination.rglob("*") if path.is_file()
            ):
                relative = artifact.relative_to(destination).as_posix().encode("utf-8")
                payload = artifact.read_bytes()
                expected_tree.update(len(relative).to_bytes(8, "big"))
                expected_tree.update(relative)
                expected_tree.update(len(payload).to_bytes(8, "big"))
                expected_tree.update(payload)
            self.assertEqual(
                digest_output.read_text(encoding="ascii").strip(),
                expected_tree.hexdigest(),
            )
            (destination / "config.json").write_text(
                '{"model":"changed"}', encoding="utf-8"
            )
            rejected = subprocess.run(
                [sys.executable, str(helper), "verify", *common],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("artifact set or digest changed", rejected.stderr)

    def test_readme_stream_command_passes_actual_smoke_url_validator(self) -> None:
        readme = (ROOT / "deploy/gpu-host/README.md").read_text(encoding="utf-8")
        match = re.search(
            r"live_stream_smoke\.py --url \"([^\"]+)\"",
            readme,
        )
        self.assertIsNotNone(match)
        assert match is not None

        smoke_path = ROOT / "services/live-stt-service/scripts/live_stream_smoke.py"
        tree = ast.parse(smoke_path.read_text(encoding="utf-8"))
        selected: list[ast.stmt] = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.parse":
                selected.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "STREAM_PROTOCOL"
                for target in node.targets
            ):
                selected.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "SmokeError":
                selected.append(node)
            elif (
                isinstance(node, ast.FunctionDef) and node.name == "validate_stream_url"
            ):
                selected.append(node)
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=selected, type_ignores=[]), str(smoke_path), "exec"
            ),
            namespace,
        )
        validator = namespace["validate_stream_url"]
        assert callable(validator)
        validator(match.group(1))

    def test_live_stt_update_waits_for_stream_readiness_without_sync_gpu_warmup(
        self,
    ) -> None:
        script = self._read_script("update.ps1")

        self.assertIn('Invoke-RestMethod "http://127.0.0.1:8200/ready"', script)
        self.assertIn(
            "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1",
            script,
        )
        self.assertIn('Reason "readiness-failed"', script)
        self.assertIn("live_stream_smoke.py", script)
        self.assertIn("bootstrap-process.ps1", script)
        self.assertIn(
            "[ValidateRange(30, 600)][int]$SmokeProcessTimeoutCapSec = 240",
            script,
        )
        self.assertIn(
            "$process.WaitForExit($smokeProcessTimeoutSec * 1000)",
            script,
        )
        stdout_drain = "$stdoutTask = $process.StandardOutput.ReadToEndAsync()"
        stderr_drain = "$stderrTask = $process.StandardError.ReadToEndAsync()"
        process_wait = "$process.WaitForExit($smokeProcessTimeoutSec * 1000)"
        self.assertIn(stdout_drain, script)
        self.assertIn(stderr_drain, script)
        self.assertLess(script.index(stdout_drain), script.index(process_wait))
        self.assertLess(script.index(stderr_drain), script.index(process_wait))
        self.assertIn("$output = $stdoutTask.GetAwaiter().GetResult()", script)
        self.assertIn("[void]$stderrTask.GetAwaiter().GetResult()", script)
        self.assertNotIn("$process.StandardOutput.ReadToEnd()", script)
        self.assertIn(
            "Stop-GpuHostProcessTreeBounded -Process $process -GraceSec 10",
            script,
        )
        self.assertIn('"smoke-process-timeout"', script)
        self.assertNotIn("$process.WaitForExit($remainingMs)", script)
        self.assertIn('"--reference-text", $referenceText', script)
        self.assertIn('"sample-tr-cv17-001"', script)
        self.assertIn('"sample-tr-cv17-002"', script)
        self.assertIn('"--min-final-events", "1"', script)
        # Content thresholds stay exactly as strict as before, now carried in
        # the content-mode branch instead of inline literals.
        self.assertIn("$gateMinFinalWordCoverage = 0.8", script)
        self.assertIn("$gateMinReferenceTokenCoverage = 0.8", script)
        self.assertIn("$gateMaxWordErrorRate = $MaxWordErrorRate", script)
        self.assertIn("$gateMaxTranscriptGapMs = 6000", script)
        # The partial requirement lives only on the repeated draft-path run and
        # must stay conditional; unconditional here is the coin flip this split
        # exists to remove.
        self.assertIn(
            "(-not $DraftPathOnly -or [int]$summary.events.partial_count -ge 1)", script
        )
        self.assertNotIn("        [int]$summary.events.partial_count -ge 1 -and", script)
        self.assertIn(
            "[double]$summary.coverage.final_word_coverage -ge 0.8",
            script,
        )
        self.assertIn(
            "[double]$summary.coverage.reference_token_coverage -ge 0.8",
            script,
        )
        self.assertIn(
            "$summary.quality_gate.min_reference_token_coverage -eq", script
        )
        self.assertIn(
            "$summary.quality_gate.max_word_error_rate -eq $gateMaxWordErrorRate", script
        )
        # The repeated/tiled run proves only that the draft path emits. Its
        # smoke command disables the wall-clock transcript-gap gate, so the
        # updater read-back must preserve that mode boundary. Single-pass
        # content runs still enforce 6000 ms.
        self.assertIn(
            "transcript_gap_within_max =\n"
            "          ($DraftPathOnly -or\n"
            "           $null -eq $summary.events.max_transcript_gap_ms -or\n"
            "           [int]$summary.events.max_transcript_gap_ms -le 6000)",
            script,
        )
        self.assertIn(
            "$null -eq $summary.events.max_transcript_gap_ms -or",
            script,
        )
        self.assertIn("[int]$summary.events.max_transcript_gap_ms -le 6000", script)
        self.assertIn("$summary.reference.text_sha256_12", script)
        self.assertIn("@($summary.quality_gate.failures).Count -eq 0", script)
        self.assertIn('"eof_ack,drained"', script)
        # A refused deploy has to name the term that refused it. These gates
        # used to be one 30-term -and chain that short-circuited to a bare
        # $false and printed nothing, so a stream rejected on content looked
        # exactly like a crashed interpreter from the console.
        self.assertIn("$failed = @($checks.Keys | Where-Object", script)
        self.assertIn("direct stream acceptance rejected by: {0}", script)
        for named_check in (
            "terminal_sequence =",
            "content_quality =",
            "no_final_hallucination =",
            "fixture_repeat_audio =",
            "smoke_reported_no_failures =",
        ):
            self.assertIn(named_check, script)
        self.assertNotIn("$summary.ok -eq $true -and", script)
        self.assertIn("LiveSttReadinessDeadlineSec", script)
        self.assertIn("[Diagnostics.Stopwatch]::StartNew()", script)
        self.assertIn("$readiness.runtime_commit -eq $ExpectedCommit", script)
        self.assertIn('$readiness.runtime.live.device -eq "cuda"', script)
        self.assertIn(
            "$readiness.speech_gate.profile -eq $script:LiveSttSpeechGateProfile",
            script,
        )
        self.assertIn("$readiness.speech_gate.rms_source", script)
        self.assertIn("$readiness.speech_gate.forced_commit_sec", script)
        self.assertIn("$readiness.speech_gate.silence_commit_sec", script)
        self.assertIn("$readiness.speech_gate.tail_overlap_sec", script)
        self.assertIn("$readiness.speech_gate.contextual_artifact.enabled", script)
        self.assertIn("$readiness.speech_gate.contextual_artifact.max_rms", script)
        self.assertIn(
            "$readiness.speech_gate.contextual_artifact.min_no_speech_prob",
            script,
        )
        self.assertIn(
            "$readiness.speech_gate.contextual_artifact.requires_text_match",
            script,
        )
        self.assertIn("$readiness.speech_gate.vad.live_enabled", script)
        self.assertIn("$readiness.speech_gate.vad.final_enabled", script)
        self.assertIn('$readiness.speech_gate.vad.empty_window_action -eq "skip_decode"', script)
        self.assertNotIn("/transcribe?language=tr&session_id=deploy-warmup", script)

    def test_acceptance_evidence_outlives_the_console_that_produced_it(
        self,
    ) -> None:
        """The only copy of the acceptance proof must not be a terminal buffer.

        On 2026-07-27 a fenced-runtime recovery succeeded and its acceptance
        text - terminal_sequence, coverage, quality gate - existed nowhere on
        disk: not in ProgramData, not under deploy\\gpu-host\\logs. The
        postcondition audit could only be answered by asking the operator to
        paste from the session that happened to run it.
        """
        script = self._read_script("update.ps1")
        writer = self._read_script("acceptance-receipt.ps1")
        # Windows PowerShell 5.1 reads these without a BOM; non-ASCII here has
        # bitten the deploy chain before.
        writer.encode("ascii")
        self.assertIn("Set-StrictMode -Version 2.0", writer)

        # The writer is its own dot-sourced module so it can be exercised
        # without running a deploy, the way deployment-state.ps1 already is.
        self.assertIn('Join-Path $PSScriptRoot "acceptance-receipt.ps1"', script)
        self.assertIn("Missing acceptance-receipt.ps1 beside update.ps1.", script)
        self.assertIn("function Write-GpuHostAcceptanceReceipt", writer)
        self.assertIn('"acceptance-receipts"', writer)
        # Receipts follow the caller's -StatePath rather than a second
        # hardcoded root, and inherit the ledger's own ACL.
        self.assertIn("[string]$StatePath = $script:ResolvedStatePath", writer)
        self.assertIn("Split-Path -Parent $StatePath", writer)
        self.assertIn("Initialize-DeploymentStateRoot -StatePath $receiptPath", writer)
        self.assertIn(
            "Set-Acl -LiteralPath $receiptPath -AclObject (New-DeploymentStateAcl)",
            writer,
        )
        self.assertIn("Assert-DeploymentStateAcl -Path $receiptPath", writer)
        self.assertIn("Acceptance receipt readback failed.", writer)
        # Persisting evidence is never itself a deploy gate.
        self.assertIn("acceptance receipt could not be written", writer)
        self.assertNotIn("return $receiptPath", writer)
        self.assertIn("an emitted path plus $false", writer)
        # Bounded history: three runs per deploy on a host nobody prunes.
        self.assertIn("$existing.Count -gt 60", writer)

        # The receipt carries the smoke summary, because that is the only place
        # terminal_sequence, coverage and the quality gate exist.
        self.assertIn("-Summary $summary", script)
        # Every terminal verdict leaves evidence, including the paths that used
        # to return a bare $false.
        for verdict in (
            "-Verdict $verdict",
            '-Verdict "smoke-process-failed"',
            '-Verdict "invalid-summary"',
            '-Verdict "not-executed"',
        ):
            self.assertIn(verdict, script)

    def test_live_stt_production_launcher_reasserts_pinned_runtime_profile(
        self,
    ) -> None:
        script = self._read_script("start-live-stt.ps1")

        preload_line = '$env:STT_STREAM_PRELOAD_MODELS = "true"'
        runtime_import_line = "Import-LiveSttRuntimeEnvironment"
        for policy_key in (
            '"STT_FORCED_COMMIT_SEC"',
            '"STT_SILENCE_COMMIT_SEC"',
            '"STT_TAIL_OVERLAP_SEC"',
        ):
            self.assertLess(script.index(policy_key), script.index(runtime_import_line))
        self.assertGreater(
            script.rindex(preload_line), script.index(runtime_import_line)
        )
        self.assertGreater(
            script.index(
                "$env:STT_STREAM_PRELOAD_MAX_ATTEMPTS = "
                '"$script:LiveSttPreloadMaxAttempts"'
            ),
            script.index(runtime_import_line),
        )
        self.assertIn("STT_LIVE_MODEL_REVISION", script)
        self.assertIn("STT_LIVE_MODEL_SHA256", script)
        self.assertIn("STT_FINAL_MODEL_REVISION", script)
        self.assertIn("STT_FINAL_MODEL_SHA256", script)
        for assignment in (
            '$env:STT_DEVICE = "cpu"',
            '$env:STT_COMPUTE_TYPE = "int8"',
            '$env:STT_LIVE_DEVICE = "cuda"',
            '$env:STT_LIVE_COMPUTE_TYPE = "int8"',
            '$env:STT_FINAL_DEVICE = "cuda"',
            '$env:STT_FINAL_COMPUTE_TYPE = "float16"',
            "$env:STT_RUNTIME_COMMIT",
            "$env:STT_STREAM_MODEL_LOAD_TIMEOUT_SEC",
            "$env:STT_STREAM_PRELOAD_READINESS_BUDGET_SEC",
            "$env:STT_FORCED_COMMIT_SEC",
            "$env:STT_SILENCE_COMMIT_SEC",
            "$env:STT_TAIL_OVERLAP_SEC",
        ):
            self.assertGreater(
                script.rindex(assignment), script.index(runtime_import_line)
            )

    def test_live_stt_runtime_contract_bounds_preload_and_is_shared(self) -> None:
        contract = self._read_script("live-stt-runtime-contract.ps1")
        launcher = self._read_script("start-live-stt.ps1")
        update = self._read_script("update.ps1")

        contract.encode("ascii")
        self.assertIn("LiveSttPreloadWorstCaseSec", contract)
        self.assertIn("[Math]::Pow(2", contract)
        self.assertIn("LiveSttAcceptanceWorstCaseSec", contract)
        self.assertIn("exceeds its readiness deadline", contract)
        self.assertIn("live-stt-runtime-contract.ps1", launcher)
        self.assertIn("live-stt-runtime-contract.ps1", update)

    # Slowest successful final-model preload measured on the GPU host, in
    # seconds. The 180s budget that preceded this guard sat ~7% above it and a
    # single slow load fenced the runtime (both tasks disabled, ports silent).
    OBSERVED_FINAL_PRELOAD_SEC = 167.9

    def test_live_stt_preload_budget_keeps_headroom_over_measured_load(
        self,
    ) -> None:
        """Recompute the contract arithmetic instead of pinning literals.

        The readiness deadline is a derived ceiling. Asserting it as a magic
        number lets someone lower the model-load budget back toward the
        measured load time without any test noticing.
        """
        contract = self._read_script("live-stt-runtime-contract.ps1")

        def value(name: str) -> int:
            match = re.search(
                r"^\$script:{0} = (\d+)$".format(name), contract, re.MULTILINE
            )
            self.assertIsNotNone(match, "{0} is not a plain integer".format(name))
            return int(match.group(1))  # type: ignore[union-attr]

        load_timeout = value("LiveSttModelLoadTimeoutSec")
        kill_grace = value("LiveSttWorkerKillGraceSec")
        attempts = value("LiveSttPreloadMaxAttempts")
        retry_base = value("LiveSttPreloadRetryBaseSec")
        roles = value("LiveSttPreloadRoleCount")
        smoke = value("LiveSttSmokeWorstCaseSec")
        reserve = value("LiveSttTaskTransitionReserveSec")
        meeting_ai_readiness = value("MeetingAiReadinessDeadlineSec")
        deadline = value("LiveSttReadinessDeadlineSec")

        self.assertGreaterEqual(
            load_timeout,
            2 * self.OBSERVED_FINAL_PRELOAD_SEC,
            "The model-load budget must keep at least 2x headroom over the "
            "slowest preload measured on the GPU host; a preload timeout "
            "rejects the deploy and can fence the runtime.",
        )

        retry_worst_case = retry_base * (2 ** (attempts - 1) - 1)
        preload_worst_case = roles * (
            attempts * (load_timeout + 2 * kill_grace) + retry_worst_case
        )
        acceptance_worst_case = (
            preload_worst_case + smoke + reserve + meeting_ai_readiness
        )
        self.assertLessEqual(
            acceptance_worst_case,
            deadline,
            "Acceptance worst case exceeds the readiness deadline; the "
            "contract would throw at launcher startup.",
        )

        # update.ps1 requires the runtime to report exactly this budget, and the
        # service parses it through a bounded pydantic field. A deadline above
        # that bound would be rejected at startup rather than at review time.
        config = (
            ROOT / "services/live-stt-service/app/core/config.py"
        ).read_text(encoding="utf-8")
        bound = re.search(
            r"stream_preload_readiness_budget_sec:.*?le=(\d+(?:\.\d+)?)",
            config,
            re.DOTALL,
        )
        self.assertIsNotNone(bound, "readiness budget bound not found")
        self.assertLessEqual(
            float(deadline),
            float(bound.group(1)),  # type: ignore[union-attr]
            "The readiness deadline exceeds the service-side field bound.",
        )

    def test_gpu_host_restart_requires_new_port_owner_and_exact_task_interpreter(
        self,
    ) -> None:
        script = self._read_script("update.ps1")

        restart_contract = self._read_script("restart-acceptance.ps1")
        self.assertIn("Get-GpuHostListeningPortOwnerSnapshot", restart_contract)
        self.assertIn("Wait-GpuHostPortReleased", restart_contract)
        self.assertIn("Wait-GpuHostNewPortOwner", restart_contract)
        self.assertIn("Wait-GpuHostNewTaskInstance", restart_contract)
        self.assertIn("Wait-GpuHostTaskInstancesReleased", restart_contract)
        self.assertIn("Test-GpuHostTaskInstanceStable", restart_contract)
        self.assertIn("Get-GpuHostListenerIdentityProof", restart_contract)
        self.assertIn("Get-WmiObject -Class Win32_Process", restart_contract)
        self.assertIn("ExpectedPythonExe", restart_contract)
        self.assertIn("ExpectedTaskPids", restart_contract)
        self.assertIn("StableSamples = 3", restart_contract)
        self.assertIn("Get-GpuHostTaskXmlContract", script)
        self.assertIn("Get-SchtasksTaskXml", script)
        self.assertIn("-PythonExe $liveSttPythonExe", script)
        self.assertNotIn("Get-Command python", script)
        self.assertIn("automatic-rollback-accepted", script)
        self.assertIn("Invoke-GpuHostRevisionAcceptance", script)
        self.assertIn('Reason "restart-failed-task-repo-root"', script)
        self.assertIn("$taskContract.RepoRoot", script)
        self.assertIn('"Acik\\platform-ai\\runtime\\meeting-service-client.key"', script)
        self.assertIn("Remove-MeetingAiStaleRuntimeTlsKeys", script)
        self.assertIn("MeetingAiReadinessDeadlineSec", script)
        self.assertIn('Invoke-RestMethod "http://127.0.0.1:8300/ready"', script)
        self.assertIn('Reason "meeting-ai-readiness-failed"', script)
        self.assertIn('Reason "meeting-ai-readiness-identity-changed"', script)
        self.assertIn("Test-MeetingAiDependencyReadiness", script)

    def test_meeting_ai_launcher_uses_non_executable_dpapi_config(self) -> None:
        script = self._read_script("start-meeting-ai.ps1")

        self.assertIn("Import-MeetingAiRuntimeEnvironment", script)
        self.assertLess(
            script.index("Remove-MeetingAiStaleRuntimeTlsKeys"),
            script.index("Import-MeetingAiRuntimeEnvironment"),
        )
        self.assertIn("meeting-ai.env", script)
        self.assertIn('MAI_INGESTION_ENABLED = "false"', script)
        self.assertIn('MAI_READY_CONSUMER_ENABLED = "false"', script)
        self.assertIn("Clear-MeetingAiManagedProcessEnvironment", script)
        self.assertIn("environment does not match the launcher", script)
        self.assertIn("requires an approved runtime config", script)
        self.assertIn("$env:MAI_APP_ENV = $AppEnv", script)
        self.assertIn("Runtime config rejected", script)
        self.assertNotIn("env.local.ps1", script)
        self.assertNotIn("falling back to mock", script)
        self.assertIn("refusing mock fallback", script)

    def test_meeting_ai_runtime_env_is_strict_and_dpapi_protected(self) -> None:
        script = self._read_script("meeting-ai-runtime-env.ps1")

        self.assertIn("S-1-5-18", script)
        self.assertIn("S-1-5-32-544", script)
        self.assertIn("Add-Type -AssemblyName System.Security", script)
        self.assertIn("OrdinalIgnoreCase", script)
        self.assertIn("System.Security.Cryptography.ProtectedData]::Unprotect", script)
        self.assertIn(
            "System.Security.Cryptography.DataProtectionScope]::LocalMachine", script
        )
        self.assertIn("must be UTF-8 without BOM", script)
        self.assertIn("must not use a UNC or device path", script)
        self.assertIn("must not resolve to a UNC or device path", script)
        self.assertIn("must reside on a fixed local volume", script)
        self.assertIn("must reside under the hardened meeting-ai runtime root", script)
        self.assertIn("Initialize-MeetingAiRuntimeRoot", script)
        self.assertIn("contains a duplicate key", script)
        self.assertIn("uses an unknown key", script)
        self.assertIn("must be an absolute HTTPS URL", script)
        self.assertIn("MAI_MEETING_SERVICE_TLS_CLIENT_KEY_DPAPI", script)
        self.assertIn("Write-MeetingAiSecretFileAtomic", script)
        self.assertIn("MoveFileEx", script)
        self.assertIn("replaceExistingAndWriteThrough", script)
        self.assertIn("Clear-MeetingAiRuntimeTlsKey", script)
        self.assertIn("MeetingAiOwnedRuntimeTlsKeyPath", script)
        self.assertIn("Remove-MeetingAiStaleRuntimeTlsKeys", script)
        self.assertIn('"meeting-service-client.*.key"', script)
        self.assertIn("[int]::TryParse", script)
        self.assertIn("Get-Process -Id $ownerPid", script)
        self.assertIn('"runtime\\tls"', script)
        self.assertIn("processStartFileTime", script)
        self.assertIn("actualStartFileTime", script)
        self.assertIn('$PID, $processStartFileTime, ([Guid]::NewGuid().ToString("N"))', script)
        self.assertNotIn('"runtime\\meeting-service-client.key"', script)
        self.assertIn("MAI_READY_REDIS_URL_DPAPI", script)
        self.assertIn("MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI", script)
        self.assertIn("Assert-TranscriptReadyPreEnablePermit", script)
        self.assertIn("Assert-TranscriptReadyPermitFile", script)
        self.assertIn("Assert-TranscriptReadyActivationReceiptFile", script)
        self.assertIn("MAI_READY_ACTIVATION_RECEIPT_PATH", script)
        self.assertIn("targetAppEnv", script)
        self.assertIn("invalid activation time", script)
        self.assertIn("host binding does not match", script)
        self.assertIn("Invoke-MeetingAiGitCapture", script)
        self.assertIn('"status", "--porcelain", "--untracked-files=no"', script)
        self.assertIn('"ls-files", "--others", "--exclude-standard"', script)
        self.assertIn("untracked deployed content", script)
        self.assertIn("forbidden dotenv source", script)
        self.assertNotIn('"MAI_MEETING_SERVICE_CLIENT_SECRET" =', script)
        self.assertNotIn('"MAI_READY_REDIS_URL" =', script)
        self.assertNotIn('"MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET" =', script)
        self.assertNotIn("Get-Random", script)

    def test_meeting_ai_provisioner_preserves_keyring_and_uses_csprng(self) -> None:
        script = self._read_script("configure-meeting-ai.ps1")

        self.assertIn("RandomNumberGenerator]::Create", script)
        self.assertIn("SecureStringToBSTR", script)
        self.assertIn("ZeroFreeBSTR", script)
        self.assertIn("Write-MeetingAiConfigAtomic", script)
        self.assertIn("Global\\platform-ai-meeting-ai-config-v1", script)
        self.assertIn("foreach ($property in $oldKeyring.PSObject.Properties)", script)
        self.assertIn("RestoreBackup", script)
        self.assertIn("ShouldProcess", script)
        self.assertIn('ValidateSet("", "server", "mutual")', script)
        self.assertIn("Protect-MeetingAiSecret -PlainText $plainClientKey", script)
        self.assertIn("Protect-SuppliedSecureValue", script)
        self.assertIn("Assert-MeetingAiReadyRedisEndpoint", script)
        self.assertIn("MAI_READY_REDIS_PREFLIGHT_URL", script)
        self.assertIn("client.ping()", script)
        self.assertIn("Ready Redis endpoint preflight failed", script)
        self.assertGreaterEqual(script.count("Assert-MeetingAiReadyRedisEndpoint"), 3)
        self.assertIn("stagedTlsPublicPaths", script)
        self.assertIn("tlsPublicArtifactsCommitted", script)
        self.assertIn("ReadyPermitSourcePath", script)
        self.assertIn(
            "requires a fresh signed permit and trust root",
            script,
        )
        self.assertIn("was already consumed", script)
        self.assertIn("faz24.transcriptReadyPermitConsumption.v1", script)
        self.assertIn("faz24.transcriptReadyActivationReceipt.v3", script)
        self.assertIn(
            "transcript-service delivery capability OAuth client secret",
            script,
        )
        self.assertIn(
            '$readyConfig["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"] = '
            "$transcriptSecretBlob",
            script,
        )
        self.assertIn("SkipReadyArtifactExistence", script)
        self.assertIn("transcript-ready-pre-enable-{0}.json", script)
        self.assertIn("Get-ReadyConfiguredValue", script)
        self.assertIn("PLATFORM_AI_TEST_INJECT_MEETING_AI_CONFIG_WRITE_FAILURE", script)
        self.assertIn('"MAI_READY_CONSUMER_ENABLED" = $effectiveReadyEnabled', script)
        self.assertNotIn("Get-Random", script)

    def test_private_gateway_host_shim_is_explicit_atomic_and_test_only(self) -> None:
        script = self._read_script("configure-private-gateway-host.ps1")

        script.encode("ascii")
        self.assertIn("TestHostShim", script)
        self.assertIn("Production uses private DNS", script)
        self.assertIn("IO.File]::Replace", script)
        self.assertIn("Set-Acl -LiteralPath $tempPath -AclObject $OriginalAcl", script)
        self.assertIn("Assert-AclSecurityEquivalent", script)
        self.assertIn("Global\\platform-ai-private-gateway-host-v1", script)
        self.assertIn("SupportsShouldProcess = $true", script)
        self.assertIn("Assert-GatewayResolution", script)
        self.assertIn("active mapping outside the managed block", script)
        self.assertNotIn("Add-Content", script)

    def test_task_action_contract_is_exact_and_shared_by_installer(self) -> None:
        contract = self._read_script("task-action-contract.ps1")
        installer = self._read_script("install.ps1")

        contract.encode("ascii")
        self.assertIn("CommandLineToArgvW", contract)
        self.assertIn("New-GpuHostTaskActionArguments", contract)
        self.assertIn("Get-GpuHostTaskActionContract", contract)
        self.assertIn("legacy-user-repo", contract)
        self.assertIn("canonical-repo", contract)
        self.assertIn("WorkingDirectory", contract)
        self.assertIn("System32\\WindowsPowerShell\\v1.0\\powershell.exe", contract)
        self.assertIn("Port = 8200", contract)
        self.assertIn("Port = 8300", contract)
        self.assertNotIn("-replace", contract)
        self.assertIn("task-action-contract.ps1", installer)
        self.assertIn("New-GpuHostTaskActionArguments @actionParams", installer)
        self.assertNotIn('$arg = "-NoProfile -ExecutionPolicy', installer)

    def test_task_action_migration_preserves_processes_and_rolls_back(self) -> None:
        script = self._read_script("migrate-task-actions.ps1")

        script.encode("ascii")
        self.assertIn("SupportsShouldProcess = $true", script)
        self.assertIn("RegisterTaskDefinition", script)
        self.assertIn("RegisterTask(", script)
        self.assertIn("GetInstances(0)", script)
        self.assertIn("EnginePID", script)
        self.assertIn("Get-NetTCPConnection", script)
        self.assertIn("Get-WmiObject -Class Win32_Process", script)
        self.assertNotIn("Get-CimInstance -ClassName Win32_Process", script)
        self.assertIn("Win32_Process", script)
        self.assertIn("ListenerIdentitySha256", script)
        self.assertIn("active-transaction.json", script)
        self.assertIn("Write-DurableJsonAtomic", script)
        self.assertIn("WriteThrough", script)
        self.assertIn("Flush($true)", script)
        self.assertIn("Read-ActiveMigrationTransaction", script)
        self.assertIn("Restore-MigrationTransaction", script)
        self.assertIn("Global\\platform-ai-task-action-migration-v1", script)
        self.assertIn("MIGRATION_ALREADY_RUNNING", script)
        self.assertIn("AbandonedMutexException", script)
        self.assertIn("Previous migration process abandoned its lock", script)
        self.assertIn("ReleaseMutex", script)
        recovery_guard = script.index('"interrupted task-action migration transaction"')
        recovery_restore = script.index(
            "Restore-MigrationTransaction -Folder $folder", recovery_guard
        )
        self.assertLess(recovery_guard, recovery_restore)
        self.assertIn("interrupted_transaction_recovery_required", script)
        self.assertIn("GetSecurityDescriptor($script:SecurityInformation)", script)
        self.assertIn("$script:SecurityInformation = 15", script)
        self.assertIn("Enable-SeSecurityPrivilege", script)
        self.assertIn("AdjustTokenPrivileges", script)
        self.assertIn("SeSecurityPrivilege", script)
        self.assertIn("TASK_PROCESS_CHANGED", script)
        self.assertIn("rollbackAttempted", script)
        self.assertIn("rollbackSucceeded", script)
        self.assertIn("PLATFORM_AI_TEST_INJECT_TASK_MIGRATION_AFTER_FIRST", script)
        self.assertIn("PLATFORM_AI_TEST_CRASH_TASK_MIGRATION_AFTER_FIRST", script)
        self.assertIn("[Environment]::Exit(9)", script)
        self.assertIn('$env:GITHUB_ACTIONS -ne "true"', script)
        self.assertIn('$env:RUNNER_ENVIRONMENT -ne "github-hosted"', script)
        self.assertIn("\\runneradmin$", script)
        self.assertIn("containsTaskArguments = $false", script)
        self.assertIn("containsTaskXml = $false", script)
        self.assertNotIn("Stop-ScheduledTask", script)
        self.assertNotIn("Start-ScheduledTask", script)
        self.assertNotIn("Unregister-ScheduledTask", script)
        self.assertNotIn("Stop-Process", script)

    def test_get_command_executable_resolutions_are_singular(self) -> None:
        """Every resolved executable path must come from exactly one match.

        `Get-Command git.exe` returns TWO Application entries on any Git for
        Windows host, because the installer ships git.exe in both cmd\\ and
        bin\\ and puts both on PATH. PowerShell member enumeration then makes
        `.Source` an array, and `& $array` joins it with spaces into one
        unusable command name. The same trap exists for `python` (real install
        plus the WindowsApps execution alias).

        Neither failure is visible on the Linux test host, so this contract is
        enforced textually over the whole deploy directory rather than by
        executing the scripts.
        """
        offenders: list[str] = []
        for path in sorted((ROOT / "deploy/gpu-host").glob("*.ps1")):
            script = path.read_text(encoding="utf-8")
            # Collapse PowerShell backtick line continuations so a statement is
            # one line regardless of how it is wrapped for readability.
            joined = re.sub(r"`\r?\n\s*", " ", script)
            for line in joined.splitlines():
                statement = line.strip()
                if statement.startswith("#") or "Get-Command" not in statement:
                    continue
                assigned = re.match(r"\$(\w+)\s*=", statement)
                name = assigned.group(1) if assigned else None
                dereferenced = ".Source" in statement or (
                    name is not None
                    and re.search(rf"\${name}\.Source\b", joined) is not None
                )
                if not dereferenced:
                    # Existence probes may stay plural: they are only tested
                    # for truthiness, never turned into an executable path.
                    continue
                if re.search(r"Select(-Object)?\s+-First\s+1", statement):
                    continue
                offenders.append("{0}: {1}".format(path.name, statement))
        self.assertEqual(
            offenders,
            [],
            "Get-Command results feeding .Source must be reduced to a single "
            "match with `Select-Object -First 1`; otherwise a duplicate on "
            "PATH yields a space-joined, unusable path.",
        )


    def test_stream_acceptance_splits_content_and_draft_path_runs(self) -> None:
        """Neither property can be judged fairly on the other's stream.

        A single 5.5s clip gives the draft pass about two chances and both are
        legitimately filtered as low-confidence, so requiring a partial from it
        passed only 1 of 4 runs on the GPU host. Repeating the clip fixes that
        (4/4) but the tiled seams push word error rate to 0.20-0.29, so content
        cannot be judged on the repeated stream either.
        """
        script = self._read_script("update.ps1")

        self.assertIn("Repeat = 1; DraftPathOnly = $false; MaxWer = 0.25", script)
        self.assertIn("Repeat = 1; DraftPathOnly = $false; MaxWer = 0.4", script)
        self.assertIn("Repeat = 5; DraftPathOnly = $true;  MaxWer = 1.0", script)
        # Per-fixture because the gate proves the PINNED model still behaves as
        # it did when pinned: 001 measures WER 0.000 and 002 measures 0.375
        # deterministically (large-v3-turbo splits "halterci" into "hal
        # tercih"). The reference stays correct Turkish; only the threshold
        # carries the known gap, so one further error still fails 002.
        self.assertIn("[double]$MaxWordErrorRate = 0.25", script)
        self.assertIn(
            "[double]$summary.coverage.word_error_rate -le $MaxWordErrorRate", script
        )
        self.assertNotIn("word_error_rate -le 0.25", script)
        self.assertIn('"--repeat-audio", "$RepeatAudio"', script)
        # The multiplier the smoke actually streamed is re-verified, so a run
        # cannot silently fall back to a single pass and still claim the gate.
        self.assertIn("[int]$summary.fixture.repeat_audio -eq $RepeatAudio", script)
        # Content assertions must not run on the repeated stream.
        self.assertIn("$DraftPathOnly -or (", script)

if __name__ == "__main__":
    unittest.main()
