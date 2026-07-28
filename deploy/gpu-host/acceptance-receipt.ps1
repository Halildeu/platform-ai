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

function Write-GpuHostAcceptanceReceipt {
  param(
    [Parameter(Mandatory = $true)][string]$Fixture,
    [Parameter(Mandatory = $true)][int]$RepeatAudio,
    [Parameter(Mandatory = $true)][bool]$DraftPathOnly,
    [Parameter(Mandatory = $true)][string]$Verdict,
    [string[]]$FailedChecks = @(),
    $Summary = $null,
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
