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
        self.assertIn("PLATFORM_AI_TEST_INJECT_RESTORE_FAILURE", script)
        self.assertNotIn("[switch]$Force", script)
        self.assertNotIn('"checkout", "-B"', script)
        self.assertNotIn("2>&1 | Out-Host", script)

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
                ["config.json", "model.bin", "tokenizer.json"],
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
        self.assertIn('"--reference-text", $referenceText', script)
        self.assertIn('"sample-tr-cv17-001"', script)
        self.assertIn('"sample-tr-cv17-002"', script)
        self.assertIn('"--min-final-word-coverage", "0.8"', script)
        self.assertIn('"--min-partial-events", "1"', script)
        self.assertIn('"--min-final-events", "1"', script)
        self.assertIn('"--min-reference-token-coverage", "0.8"', script)
        self.assertIn('"--max-word-error-rate", "0.25"', script)
        self.assertIn('"--max-transcript-gap-ms", "6000"', script)
        self.assertNotIn('"--min-final-word-coverage", "0"', script)
        self.assertNotIn('"--min-partial-events", "0"', script)
        self.assertNotIn('"--max-transcript-gap-ms", "0"', script)
        self.assertIn("[int]$summary.events.partial_count -ge 1", script)
        self.assertIn(
            "[double]$summary.coverage.final_word_coverage -ge 0.8",
            script,
        )
        self.assertIn(
            "[double]$summary.coverage.reference_token_coverage -ge 0.8",
            script,
        )
        self.assertIn(
            "[double]$summary.coverage.word_error_rate -le 0.25",
            script,
        )
        self.assertIn(
            "$summary.quality_gate.min_reference_token_coverage -eq 0.8",
            script,
        )
        self.assertIn("$summary.quality_gate.max_word_error_rate -eq 0.25", script)
        self.assertIn(
            "[int]$summary.events.max_transcript_gap_ms -le 6000",
            script,
        )
        self.assertIn("$summary.reference.text_sha256_12", script)
        self.assertIn("@($summary.quality_gate.failures).Count -eq 0", script)
        self.assertIn('"eof_ack,drained"', script)
        self.assertIn("LiveSttReadinessDeadlineSec", script)
        self.assertIn("[Diagnostics.Stopwatch]::StartNew()", script)
        self.assertIn("$readiness.runtime_commit -eq $ExpectedCommit", script)
        self.assertIn('$readiness.runtime.live.device -eq "cuda"', script)
        self.assertNotIn("/transcribe?language=tr&session_id=deploy-warmup", script)

    def test_live_stt_production_launcher_reasserts_pinned_runtime_profile(
        self,
    ) -> None:
        script = self._read_script("start-live-stt.ps1")

        preload_line = '$env:STT_STREAM_PRELOAD_MODELS = "true"'
        runtime_import_line = "Import-LiveSttRuntimeEnvironment"
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
        self.assertIn("LiveSttReadinessDeadlineSec = 960", contract)
        self.assertIn("LiveSttAcceptanceWorstCaseSec", contract)
        self.assertIn("exceeds its readiness deadline", contract)
        self.assertIn("live-stt-runtime-contract.ps1", launcher)
        self.assertIn("live-stt-runtime-contract.ps1", update)

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

    def test_meeting_ai_launcher_uses_non_executable_dpapi_config(self) -> None:
        script = self._read_script("start-meeting-ai.ps1")

        self.assertIn("Import-MeetingAiRuntimeEnvironment", script)
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


if __name__ == "__main__":
    unittest.main()
