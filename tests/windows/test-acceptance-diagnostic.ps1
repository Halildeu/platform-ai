$ErrorActionPreference = 'Stop'
$source = Get-Content (Join-Path $PSScriptRoot '..\..\deploy\gpu-host\update.ps1') -Raw
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Updater parse failed.' }
$definition = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Write-GpuHostAcceptanceDiagnostic'}, $true)
Invoke-Expression $definition.Extent.Text
$sha = 'a' * 40
$originalOut = [Console]::Out
try {
    foreach ($case in @(
        @{Input='readiness-failed';Expected='readiness-failed'},
        @{Input='acceptance-exception-InvalidOperationException';Expected='acceptance-exception'},
        @{Input='private diagnostic text must not escape';Expected='acceptance-reason-unavailable'}
    )) {
        $writer = New-Object IO.StringWriter
        [Console]::SetOut($writer)
        Write-GpuHostAcceptanceDiagnostic -CandidateCommit $sha -Reason $case.Input
        $line = $writer.ToString().Trim()
        $prefix = 'FAZ24_GPU_ACCEPTANCE_REASON:'
        if (-not $line.StartsWith($prefix)) { throw 'Diagnostic marker missing.' }
        $payload = $line.Substring($prefix.Length) | ConvertFrom-Json
        if ($payload.candidateCommit -cne $sha -or $payload.reason -cne $case.Expected -or
            $payload.schemaVersion -cne 'faz24.gpu-acceptance-diagnostic.v1' -or
            @($payload.PSObject.Properties).Count -ne 3) { throw 'Diagnostic contract failed.' }
        $writer.Dispose()
    }
} finally { [Console]::SetOut($originalOut) }
try { Write-GpuHostAcceptanceDiagnostic -CandidateCommit 'main' -Reason 'readiness-failed'; throw 'invalid candidate accepted' }
catch { if ($_.Exception.Message -notlike '*candidate identity is invalid*') { throw } }
Write-Host 'Acceptance diagnostic metadata contract: PASS'
