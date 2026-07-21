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
        self.assertIn('[string]$RepoRoot = ""', script)
        self.assertNotIn("Split-Path -Parent (Split-Path -Parent $PSScriptRoot)", script)
        self.assertNotIn("$Branch..HEAD", script)
        self.assertNotIn("origin/$Branch..HEAD", script)
        self.assertNotIn('"origin/$Branch"', script)

    def test_update_script_is_ps51_safe(self) -> None:
        script = self._read_script("update.ps1")

        self._assert_ps51_safe_script(script)
        self.assertIn("function Invoke-GitStream", script)
        self.assertIn('$unpushedRange = "{0}..HEAD" -f $originRef', script)
        self.assertIn('[string]$TargetCommit', script)
        self.assertIn("exactly 40 hex characters", script)
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
        self.assertIn("$StatePath -ne $script:DefaultDeploymentStatePath", script)
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

    def test_live_stt_start_sets_cold_load_timeout_before_local_overrides(self) -> None:
        script = self._read_script("start-live-stt.ps1")

        timeout_line = '$env:STT_REQUEST_TIMEOUT = "180"'
        self.assertIn(timeout_line, script)
        self.assertLess(script.index(timeout_line), script.index("$envLocal = Join-Path"))

    def test_live_stt_update_waits_for_stream_readiness_without_sync_gpu_warmup(self) -> None:
        script = self._read_script("update.ps1")

        self.assertIn('Invoke-RestMethod "http://127.0.0.1:8200/ready"', script)
        self.assertIn(
            "ws://127.0.0.1:8200/ws/stream?protocol=source-ranges-v1",
            script,
        )
        self.assertIn('Set-DeploymentLedgerResult -Result "readiness-failed"', script)
        self.assertIn("live_stream_smoke.py", script)
        self.assertIn("--min-final-events 1", script)
        self.assertIn('"eof_ack,drained"', script)
        self.assertIn("within 750s", script)
        self.assertNotIn("/transcribe?language=tr&session_id=deploy-warmup", script)

    def test_live_stt_production_launcher_reasserts_preload_after_local_overrides(self) -> None:
        script = self._read_script("start-live-stt.ps1")

        preload_line = '$env:STT_STREAM_PRELOAD_MODELS = "true"'
        env_local_line = "$envLocal = Join-Path"
        self.assertGreater(script.rindex(preload_line), script.index(env_local_line))
        self.assertGreater(
            script.index('$env:STT_STREAM_PRELOAD_MAX_ATTEMPTS = "2"'),
            script.index(env_local_line),
        )
        self.assertIn("STT_LIVE_MODEL_REVISION", script)
        self.assertIn("STT_LIVE_MODEL_SHA256", script)
        self.assertIn("STT_FINAL_MODEL_REVISION", script)
        self.assertIn("STT_FINAL_MODEL_SHA256", script)
        self.assertIn('$env:STT_DEVICE = "cpu"', script)

    def test_meeting_ai_launcher_uses_non_executable_dpapi_config(self) -> None:
        script = self._read_script("start-meeting-ai.ps1")

        self.assertIn("Import-MeetingAiRuntimeEnvironment", script)
        self.assertIn("meeting-ai.env", script)
        self.assertIn('MAI_INGESTION_ENABLED = "false"', script)
        self.assertIn('MAI_READY_CONSUMER_ENABLED = "false"', script)
        self.assertIn("Clear-MeetingAiManagedProcessEnvironment", script)
        self.assertIn("environment does not match the launcher", script)
        self.assertIn("requires an approved runtime config", script)
        self.assertIn('$env:MAI_APP_ENV = $AppEnv', script)
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
        self.assertIn("System.Security.Cryptography.DataProtectionScope]::LocalMachine", script)
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
            'transcript-service delivery capability OAuth client secret',
            script,
        )
        self.assertIn(
            '$readyConfig["MAI_TRANSCRIPT_SERVICE_CLIENT_SECRET_DPAPI"] = '
            '$transcriptSecretBlob',
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
        self.assertIn('\\runneradmin$', script)
        self.assertIn("containsTaskArguments = $false", script)
        self.assertIn("containsTaskXml = $false", script)
        self.assertNotIn("Stop-ScheduledTask", script)
        self.assertNotIn("Start-ScheduledTask", script)
        self.assertNotIn("Unregister-ScheduledTask", script)
        self.assertNotIn("Stop-Process", script)


if __name__ == "__main__":
    unittest.main()
