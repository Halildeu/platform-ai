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

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
. (Join-Path $repoRoot 'deploy\gpu-host\acceptance-receipt.ps1')
$privateMarker = 'PRIVATE-CONTENT-DO-NOT-PERSIST'
$errorJson = '{"schema":"platform-ai.live-stt.stream-smoke.error.v1","ok":false,"error_code":"smoke_contract_failed","error_class":"SmokeError","failure_stage":"ready","message":"PRIVATE-CONTENT-DO-NOT-PERSIST"}'
$diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $errorJson `
    -StandardError "Traceback at C:\$privateMarker\file.py`nFileNotFoundError: $privateMarker" `
    -ExitCode 1 -DeadlineOpen $true
if ($diagnostic.errorCode -cne 'smoke_contract_failed' -or
    $diagnostic.failureStage -cne 'ready' -or
    $diagnostic.errorClass -cne 'SmokeError' -or
    $diagnostic.stderrExceptionClass -cne 'FileNotFoundError' -or
    $diagnostic.exitCode -ne 1 -or -not $diagnostic.deadlineOpen -or
    ($diagnostic | ConvertTo-Json -Depth 8) -match $privateMarker) {
    throw 'Nonzero smoke error projection lost metadata or leaked private content.'
}
$unknownStage = $errorJson.Replace('"ready"', '"PRIVATE-CONTENT-DO-NOT-PERSIST"').Replace('"SmokeError"', '"PRIVATE-CONTENT-DO-NOT-PERSIST"')
$diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $unknownStage `
    -ExitCode 1 -DeadlineOpen $true
if ($null -ne $diagnostic.failureStage -or $null -ne $diagnostic.errorClass -or
    ($diagnostic | ConvertTo-Json -Depth 8) -match $privateMarker) {
    throw 'Unrecognized failure stage must never reach the receipt.'
}
$summaryJson = @'
{"schema":"platform-ai.live-stt.stream-smoke.v1","ok":false,"url":"https://PRIVATE-CONTENT-DO-NOT-PERSIST","events":{"partial_count":2,"final_count":1,"error_count":0,"terminal_sequence":["eof_ack","drained","PRIVATE-CONTENT-DO-NOT-PERSIST"],"text":"PRIVATE-CONTENT-DO-NOT-PERSIST"},"coverage":{"word_error_rate":0.375,"final_words":"PRIVATE-CONTENT-DO-NOT-PERSIST","reference_words":true},"quality_gate":{"failures":["word_error_rate_above_max","PRIVATE-CONTENT-DO-NOT-PERSIST"]},"errors":["PRIVATE-CONTENT-DO-NOT-PERSIST"]}
'@
$handshakeJson = '{"schema":"platform-ai.live-stt.stream-smoke.error.v1","ok":false,"error_code":"smoke_internal_failed","error_class":"InvalidStatus","http_status":403,"headers":{"Authorization":"PRIVATE-CONTENT-DO-NOT-PERSIST"}}'
$diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $handshakeJson `
    -ExitCode 1 -DeadlineOpen $true
if ($diagnostic.errorClass -cne 'InvalidStatus' -or $diagnostic.httpStatus -ne 403 -or
    ($diagnostic | ConvertTo-Json -Depth 8) -match $privateMarker) {
    throw 'Handshake diagnostic must retain bounded HTTP status without headers.'
}
foreach ($invalidStatus in @('true', '"403"', '99', '600', '403.5', 'null')) {
    $invalidHandshake = $handshakeJson.Replace('"http_status":403', ('"http_status":' + $invalidStatus))
    $diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $invalidHandshake `
        -ExitCode 1 -DeadlineOpen $true
    if ($null -ne $diagnostic.httpStatus) { throw 'HTTP status must be a bounded integer.' }
}
$diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $summaryJson `
    -ExitCode 1 -DeadlineOpen $true
if ($diagnostic.stdoutShape -cne 'smoke-summary' -or
    $diagnostic.metrics['coverage.word_error_rate'] -ne 0.375 -or
    $diagnostic.metrics.Contains('coverage.final_words') -or
    $diagnostic.metrics.Contains('coverage.reference_words') -or
    ($diagnostic.terminalSequence -join ',') -cne 'eof_ack,drained,unrecognized' -or
    ($diagnostic.qualityFailures -join ',') -cne 'word_error_rate_above_max,unrecognized' -or
    ($diagnostic | ConvertTo-Json -Depth 8) -match $privateMarker) {
    throw 'Failure summary must preserve allowlisted metrics only.'
}
foreach ($case in @(
    @{ Out = ''; Shape = 'empty'; Err = ''; Class = $null },
    @{ Out = $privateMarker; Shape = 'invalid-json'; Err = $privateMarker; Class = $null },
    @{ Out = ('x' * 65537); Shape = 'oversized'; Err = ('x' * 65537); Class = $null },
    @{ Out = '{"schema":"unknown"}'; Shape = 'unrecognized-json'; Err = "usage: private.py`nprivate.py: error: $privateMarker"; Class = 'ArgumentParserError' },
    @{ Out = '{"schema":["platform-ai.live-stt.stream-smoke.error.v1"],"ok":false}'; Shape = 'unrecognized-json'; Err = "PrivateException: $privateMarker"; Class = $null }
)) {
    $diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $case.Out `
        -StandardError $case.Err -ExitCode 2 -DeadlineOpen $false
    if ($diagnostic.stdoutShape -cne $case.Shape -or
        $diagnostic.stderrExceptionClass -cne $case.Class -or
        $diagnostic.deadlineOpen -or
        ($diagnostic | ConvertTo-Json -Depth 8) -match $privateMarker) {
        throw 'Malformed/oversized/unknown failure metadata must remain bounded and private.'
    }
}

. (Join-Path $repoRoot 'deploy\gpu-host\deployment-state.ps1')
$receiptRoot = Join-Path ([IO.Path]::GetTempPath()) ('smoke-receipt-' + [guid]::NewGuid())
try {
    $diagnostic = ConvertTo-GpuHostSmokeFailureDiagnostic -StandardOutput $errorJson `
        -StandardError "FileNotFoundError: $privateMarker" -ExitCode 1 -DeadlineOpen $true
    $emitted = @(Write-GpuHostAcceptanceReceipt -Fixture 'sample-tr-cv17-001' `
        -RepeatAudio 1 -DraftPathOnly $false -Verdict 'smoke-process-failed' `
        -FailedChecks @('smoke_exit_code_or_deadline') -FailureDiagnostic $diagnostic `
        -StatePath (Join-Path $receiptRoot 'state.json'))
    $files = @(Get-ChildItem (Join-Path $receiptRoot 'acceptance-receipts') -File)
    if ($emitted.Count -ne 0 -or $files.Count -ne 1) {
        throw 'Receipt persistence must not emit truthy pipeline output.'
    }
    Assert-DeploymentStateAcl -Path $files[0].FullName
    $content = Get-Content $files[0].FullName -Raw
    $readback = $content | ConvertFrom-Json
    if ($readback.verdict -cne 'smoke-process-failed' -or $null -ne $readback.summary -or
        $readback.failureDiagnostic.exitCode -ne 1 -or
        $readback.failureDiagnostic.errorCode -cne 'smoke_contract_failed' -or
        $readback.failureDiagnostic.stderrExceptionClass -cne 'FileNotFoundError' -or
        $content -match $privateMarker) {
        throw 'Persisted failure receipt must retain only safe diagnostic metadata.'
    }
} finally {
    if (Test-Path -LiteralPath $receiptRoot) {
        Remove-Item -LiteralPath $receiptRoot -Recurse -Force
    }
}

# Exercise the actual updater ProcessStartInfo branch using a local synthetic
# child. Only receipt persistence and the explicit deadline-negative case are mocked.
$fixtureDefinition = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-LiveSttFixtureAcceptance'}, $true)
Invoke-Expression $fixtureDefinition.Extent.Text
foreach ($functionSource in @(
    @{ Path = 'deploy\gpu-host\task-action-contract.ps1'; Name = 'ConvertTo-GpuHostWindowsArgument' },
    @{ Path = 'deploy\gpu-host\restart-acceptance.ps1'; Name = 'Test-GpuHostDeadlineOpen' }
)) {
    $functionAst = [Management.Automation.Language.Parser]::ParseInput(
        (Get-Content (Join-Path $repoRoot $functionSource.Path) -Raw), [ref]$tokens, [ref]$errors)
    $functionName = $functionSource.Name
    $definition = $functionAst.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName}, $true)
    Invoke-Expression $definition.Extent.Text
}
function Write-GpuHostAcceptanceReceipt {
    param($Fixture, $RepeatAudio, $DraftPathOnly, $Verdict, $FailedChecks, $Summary, $FailureDiagnostic)
    $script:CapturedSmokeReceipt = [pscustomobject]@{
        verdict = $Verdict; summary = $Summary; diagnostic = $FailureDiagnostic
    }
}
$controllerRoot = Join-Path ([IO.Path]::GetTempPath()) ('smoke diagnostic ' + [guid]::NewGuid())
$pythonExe = (Get-Command python -ErrorAction Stop).Source
try {
    $scripts = Join-Path $controllerRoot 'services\live-stt-service\scripts'
    $fixtures = Join-Path $controllerRoot 'services\live-stt-service\tests\fixtures'
    [IO.Directory]::CreateDirectory($scripts) | Out-Null
    [IO.Directory]::CreateDirectory($fixtures) | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixtures 'sample-tr-cv17-001.wav'), 'synthetic')
    [IO.File]::WriteAllText((Join-Path $fixtures 'sample-tr-cv17-001.txt'), 'synthetic')
    foreach ($case in @(
        @{ Out = $errorJson; Err = ''; Exit = 1; Shape = 'smoke-error'; Closed = $false },
        @{ Out = $handshakeJson; Err = ''; Exit = 1; Shape = 'smoke-error'; Closed = $false },
        @{ Out = $summaryJson; Err = ''; Exit = 1; Shape = 'smoke-summary'; Closed = $false },
        @{ Out = $summaryJson.Replace('"ok":false', '"ok":true'); Err = ''; Exit = 1; Shape = 'smoke-summary'; Closed = $false },
        @{ Out = ''; Err = "ModuleNotFoundError: $privateMarker"; Exit = 1; Shape = 'empty'; Closed = $false },
        @{ Out = $privateMarker; Err = $privateMarker; Exit = 2; Shape = 'invalid-json'; Closed = $false },
        @{ Out = $errorJson; Err = ''; Exit = 0; Shape = 'smoke-error'; Closed = $true }
    )) {
        $body = "import sys`nsys.stdout.write(" + ($case.Out | ConvertTo-Json -Compress) + ")`n" +
            'sys.stderr.write(' + ($case.Err | ConvertTo-Json -Compress) + ")`n" +
            "raise SystemExit($($case.Exit))`n"
        [IO.File]::WriteAllText((Join-Path $scripts 'live_stream_smoke.py'), $body)
        if ($case.Closed) {
            function Test-GpuHostDeadlineOpen { param($Clock, $DeadlineSec) return $false }
        }
        $script:CapturedSmokeReceipt = $null
        $result = Invoke-LiveSttFixtureAcceptance -PythonExe $pythonExe `
            -Clock ([Diagnostics.Stopwatch]::StartNew()) -DeadlineSec 30 `
            -FixtureBaseName 'sample-tr-cv17-001'
        $receipt = $script:CapturedSmokeReceipt
        if ($result -isnot [bool] -or $result -ne $false -or
            $receipt.verdict -cne 'smoke-process-failed' -or $null -ne $receipt.summary -or
            $receipt.diagnostic.exitCode -ne $case.Exit -or
            $receipt.diagnostic.stdoutShape -cne $case.Shape -or
            $receipt.diagnostic.deadlineOpen -eq $case.Closed -or
            ($receipt | ConvertTo-Json -Depth 8) -match $privateMarker) {
            throw 'Updater child failure must remain false and retain privacy-safe metadata.'
        }
    }
} finally {
    if (Test-Path -LiteralPath $controllerRoot) {
        Remove-Item -LiteralPath $controllerRoot -Recurse -Force
    }
}
Write-Host 'Nonzero child smoke privacy-safe diagnostic contract: PASS'
