# Durable deploy-acceptance evidence, dot-sourced by update.ps1.
#
# Acceptance evidence has to outlive the console that produced it. The smoke
# summary is the only place terminal_sequence (eof_ack,drained), coverage and
# the quality gate exist, and it used to be parsed, judged and dropped - so a
# postcondition audit could only be answered by whoever still had the deploy
# session open. On 2026-07-27 that cost a real audit: a fenced-runtime recovery
# succeeded but its acceptance text existed nowhere on disk, neither in
# ProgramData nor under deploy\gpu-host\logs, and had to be requested back from
# the operator who ran it.
#
# Receipts land beside the ledger under the same SYSTEM + Administrators-only
# ACL. Requires deployment-state.ps1 to be dot-sourced first.
#
# Writing a receipt is never a deploy gate: evidence that fails to persist is
# reported loudly and the acceptance verdict itself is left untouched. A deploy
# must not be refused because its paperwork could not be filed.

Set-StrictMode -Version 2.0

function Get-GpuHostSmokeField {
  param($Object, [string]$Name)
  if ($null -eq $Object) { return $null }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -ne $property) { return ,$property.Value }
  return $null
}

function ConvertTo-GpuHostSmokeFailureDiagnostic {
  param(
    [string]$StandardOutput = "",
    [string]$StandardError = "",
    [int]$ExitCode,
    [bool]$DeadlineOpen
  )
  # Child output is untrusted, even for an allowlisted fixture. Never persist
  # exception messages, traceback paths, URL fields or arbitrary JSON members.
  $diagnostic = [ordered]@{
    schemaVersion = "platform-ai.smoke-failure-diagnostic.v1"
    exitCode = $ExitCode
    deadlineOpen = $DeadlineOpen
    stdoutShape = "empty"
    stderrShape = "empty"
    errorCode = $null
    errorClass = $null
    failureStage = $null
    stderrExceptionClass = $null
    reportedOk = $null
    metrics = [ordered]@{}
    terminalSequence = @()
    qualityFailures = @()
  }
  if (-not [string]::IsNullOrEmpty($StandardError)) {
    $diagnostic.stderrShape = "present"
    if ($StandardError.Length -gt 65536) {
      $diagnostic.stderrShape = "oversized"
    } else {
      $classes = @("ModuleNotFoundError", "ImportError", "FileNotFoundError",
        "PermissionError", "ConnectionRefusedError", "ConnectionResetError",
        "ConnectionClosedError", "ConnectionClosedOK", "TimeoutError",
        "OSError", "RuntimeError", "ValueError", "TypeError", "KeyError",
        "AttributeError", "UnicodeDecodeError", "UnicodeEncodeError",
        "JSONDecodeError", "SyntaxError", "IndentationError", "MemoryError")
      $pattern = '(?m)^[ \t]*(' + ($classes -join '|') + ')(?=:|\r?$)'
      $matches = [regex]::Matches($StandardError, $pattern)
      if ($matches.Count -gt 0) {
        $diagnostic.stderrExceptionClass = $matches[$matches.Count - 1].Groups[1].Value
      } elseif ($StandardError -match '(?m)^usage:' -and
          $StandardError -match '(?m)^[^\r\n]*: error:') {
        $diagnostic.stderrExceptionClass = "ArgumentParserError"
      }
    }
  }
  if ([string]::IsNullOrWhiteSpace($StandardOutput)) { return [pscustomobject]$diagnostic }
  if ($StandardOutput.Length -gt 65536) {
    $diagnostic.stdoutShape = "oversized"
    return [pscustomobject]$diagnostic
  }
  try {
    $summary = $StandardOutput | ConvertFrom-Json -ErrorAction Stop
    $schema = Get-GpuHostSmokeField $summary "schema"
    $ok = Get-GpuHostSmokeField $summary "ok"
    if ($ok -is [bool]) { $diagnostic.reportedOk = $ok }
    if ($schema -is [string] -and $schema -ceq "platform-ai.live-stt.stream-smoke.error.v1") {
      $diagnostic.stdoutShape = "smoke-error"
      $code = Get-GpuHostSmokeField $summary "error_code"
      if ($code -is [string] -and $code -cin @("smoke_contract_failed", "smoke_internal_failed")) {
        $diagnostic.errorCode = $code
      }
      $errorClass = Get-GpuHostSmokeField $summary "error_class"
      if ($errorClass -is [string] -and $errorClass -cin @("SmokeError", "unclassified",
          "ModuleNotFoundError", "ImportError", "FileNotFoundError", "PermissionError",
          "ConnectionRefusedError", "ConnectionResetError", "ConnectionClosedError",
          "ConnectionClosedOK", "TimeoutError", "OSError", "RuntimeError", "ValueError",
          "TypeError", "KeyError", "AttributeError", "UnicodeDecodeError", "UnicodeEncodeError",
          "JSONDecodeError", "MemoryError")) {
        $diagnostic.errorClass = $errorClass
      }
      $stage = Get-GpuHostSmokeField $summary "failure_stage"
      if ($stage -is [string] -and $stage -cin @("argument", "fixture", "ready",
          "transcript", "terminal", "stream", "unclassified")) {
        $diagnostic.failureStage = $stage
      }
    } elseif ($schema -is [string] -and $schema -ceq "platform-ai.live-stt.stream-smoke.v1") {
      $diagnostic.stdoutShape = "smoke-summary"
      foreach ($group in @(
          @{ Name = "events"; Fields = @("partial_count", "final_count",
              "final_hallucination_count", "error_count", "max_transcript_gap_ms") },
          @{ Name = "coverage"; Fields = @("final_words", "reference_words",
              "final_word_coverage", "reference_token_coverage", "word_error_rate") },
          @{ Name = "latency"; Fields = @("ready_ms", "elapsed_ms") }
        )) {
        $container = Get-GpuHostSmokeField $summary $group.Name
        foreach ($field in $group.Fields) {
          $value = Get-GpuHostSmokeField $container $field
          if (($value -is [int] -or $value -is [long] -or $value -is [double] -or
              $value -is [decimal]) -and $value -ge 0 -and $value -le 1000000000) {
            $diagnostic.metrics["$($group.Name).$field"] = $value
          }
        }
      }
      $events = Get-GpuHostSmokeField $summary "events"
      $terminal = Get-GpuHostSmokeField $events "terminal_sequence"
      $diagnostic.terminalSequence = @($terminal | Select-Object -First 4 | ForEach-Object {
        if ($_ -is [string] -and $_ -cin @("eof_ack", "drained")) { $_ }
        else { "unrecognized" }
      })
      $gate = Get-GpuHostSmokeField $summary "quality_gate"
      $allowedFailures = @("ready_missing", "error_events_present", "terminal_sequence_invalid",
        "final_event_count_below_min", "partial_event_count_below_min",
        "final_hallucination_detected", "final_word_coverage_below_min",
        "reference_quality_unavailable", "reference_token_coverage_below_min",
        "word_error_rate_above_max", "transcript_event_gap_above_max")
      $failures = Get-GpuHostSmokeField $gate "failures"
      $diagnostic.qualityFailures = @($failures |
        Select-Object -First 16 | ForEach-Object {
          if ($_ -is [string] -and $_ -cin $allowedFailures) { $_ }
          else { "unrecognized" }
        })
    } else {
      $diagnostic.stdoutShape = "unrecognized-json"
    }
  } catch {
    $diagnostic.stdoutShape = "invalid-json"
  }
  return [pscustomobject]$diagnostic
}

function Write-GpuHostAcceptanceReceipt {
  param(
    [Parameter(Mandatory = $true)][string]$Fixture,
    [Parameter(Mandatory = $true)][int]$RepeatAudio,
    [Parameter(Mandatory = $true)][bool]$DraftPathOnly,
    [Parameter(Mandatory = $true)][string]$Verdict,
    [string[]]$FailedChecks = @(),
    $Summary = $null,
    $FailureDiagnostic = $null,
    # Follows the caller's -StatePath instead of a second hardcoded root, so a
    # test or a non-default ledger location keeps its receipts together with
    # the ledger it belongs to.
    [string]$StatePath = $script:ResolvedStatePath
  )

  try {
    if ([string]::IsNullOrWhiteSpace($StatePath)) {
      Write-Host "[update] acceptance receipt skipped: state path unresolved" `
        -ForegroundColor Yellow
      return $null
    }
    $receiptDir = Join-Path (Split-Path -Parent $StatePath) "acceptance-receipts"
    $name = "{0}-{1}-r{2}.json" -f `
      ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfff")), $Fixture, $RepeatAudio
    $receiptPath = Join-Path $receiptDir $name
    # Creates and hardens the parent directory with the ledger's own ACL.
    Initialize-DeploymentStateRoot -StatePath $receiptPath | Out-Null

    # The smoke summary is redaction-safe by construction: it carries event
    # counts, latencies, word counts and short hashes, never transcript text.
    # See services/live-stt-service/scripts/live_stream_smoke.py.
    $receipt = [pscustomobject][ordered]@{
      schemaVersion = 1
      kind          = "platform-ai.gpu-host.acceptance-receipt"
      createdAtUtc  = [DateTime]::UtcNow.ToString("o")
      fixture       = $Fixture
      repeatAudio   = $RepeatAudio
      draftPathOnly = $DraftPathOnly
      verdict       = $Verdict
      failedChecks  = @($FailedChecks)
      summary       = $Summary
      failureDiagnostic = $FailureDiagnostic
    }
    [IO.File]::WriteAllText(
      $receiptPath,
      ($receipt | ConvertTo-Json -Depth 8),
      (New-Object Text.UTF8Encoding($false))
    )
    Set-Acl -LiteralPath $receiptPath -AclObject (New-DeploymentStateAcl)
    Assert-DeploymentStateAcl -Path $receiptPath
    $readback = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ("$($readback.verdict)" -ne $Verdict) {
      throw "Acceptance receipt readback failed."
    }
    Write-Host ("[update] acceptance receipt: {0}" -f $receiptPath)

    # Bounded history: one receipt per fixture run, three runs per deploy. 60
    # keeps roughly the last twenty deploys and stops unbounded growth on a
    # host nobody prunes by hand. Only our own file shape is ever removed.
    $existing = @(Get-ChildItem -LiteralPath $receiptDir -File `
      -Filter "*-r*.json" -ErrorAction SilentlyContinue |
      Sort-Object Name -Descending)
    if ($existing.Count -gt 60) {
      foreach ($stale in $existing[60..($existing.Count - 1)]) {
        Remove-Item -LiteralPath $stale.FullName -Force -ErrorAction SilentlyContinue
      }
    }
    # Do not emit the path to the success pipeline. Callers use this writer
    # inside boolean acceptance expressions; an emitted path plus $false would
    # become a truthy object array and bypass a rejected smoke gate.
    return
  } catch {
    Write-Host ("[update] acceptance receipt could not be written: {0}" -f `
      $_.Exception.GetType().Name) -ForegroundColor Yellow
    return $null
  }
}
