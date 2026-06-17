<#
.SYNOPSIS
  #161 (T-B) diarization baseline sweep — builds n>1 synthetic fixtures and runs
  diar_matrix.py once over all of them, so the DER row is an AVERAGE over several
  conversations instead of a single n=1 smoke number.

.DESCRIPTION
  Pure-measurement helper for the GPU host (RTX 4070). It:
    1. regenerates a diverse synthetic fixture set (varying seed / speaker-count /
       speaker-offset → distinct CV-TR voices and turn orders),
    2. runs scripts/diar_matrix.py ONCE over the whole dir (diar_matrix globs all
       wav+rttm pairs → n = number of fixtures),
    3. appends the single JSON result row to the evidence file.

  stdout (the JSON row) is captured via cmd.exe redirection so PowerShell never
  mistakes the script's stderr progress lines for errors (same trap fixed in #136).
  No fake numbers are produced here — this only runs the real model and records
  whatever it measures.

.PARAMETER Backend
  pyannote (default) or speechbrain. The backend's deps must already be installed
  in the active venv (requirements-pyannote.txt / requirements-speechbrain.txt).

.PARAMETER Src
  Directory of single-speaker CV-TR wavs used to assemble conversations.

.EXAMPLE
  # from services\diarization-service, inside the .venv-diar venv, DIA_HF_TOKEN set:
  .\scripts\diar_sweep.ps1 -Backend pyannote -Tag pyannote-3.1
#>
[CmdletBinding()]
param(
    [ValidateSet("pyannote", "speechbrain")]
    [string]$Backend = "pyannote",

    [string]$Src = "..\live-stt-service\tests\fixtures",
    [string]$Dst = "tests\fixtures\diar-tr",
    [string]$Tag = "",
    [string]$EvidenceFile = "..\..\docs\evidence\diar-results-$(Get-Date -Format yyyy-MM-dd).jsonl",
    [string]$Model = "pyannote/speaker-diarization-3.1"
)

$ErrorActionPreference = "Stop"
if (-not $Tag) { $Tag = "$Backend-sweep" }

# Adapt to however many distinct single-speaker voices the source has: with only
# 2 voices we still build several DISTINCT 2-speaker conversations by varying the
# seed / turn-count / gap (different order + length → real DER variance, n>1).
# If ≥3 voices exist, some 3-speaker conversations are added automatically.
$srcWavCount = @(Get-ChildItem -Path (Join-Path $Src "*.wav") -ErrorAction SilentlyContinue).Count
if ($srcWavCount -lt 2) {
    Write-Error "need >=2 single-speaker wavs in $Src (found $srcWavCount)"
    exit 2
}
$maxSpk = [Math]::Min(3, $srcWavCount)
Write-Host "== diar_sweep: backend=$Backend, source voices=$srcWavCount (maxSpeakers=$maxSpk)" -ForegroundColor Cyan

# (seed, num-speakers, turns, gap) — seed is the unique filename key. n is capped
# at the available voices so nothing is skipped for lack of speakers.
$combos = @(
    @{ seed = 11; n = 2; turns = 8; gap = 0.40 },
    @{ seed = 12; n = 2; turns = 10; gap = 0.30 },
    @{ seed = 13; n = 2; turns = 6; gap = 0.60 },
    @{ seed = 14; n = $maxSpk; turns = 9; gap = 0.40 },
    @{ seed = 15; n = $maxSpk; turns = 12; gap = 0.50 },
    @{ seed = 16; n = 2; turns = 9; gap = 0.35 }
)

Write-Host "== building $($combos.Count) fixtures into $Dst" -ForegroundColor Cyan

# Fresh fixture dir so n reflects exactly this sweep (old files would inflate n).
if (Test-Path $Dst) { Remove-Item "$Dst\synthetic-diar-*.wav", "$Dst\synthetic-diar-*.rttm" -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Dst | Out-Null

$built = 0
foreach ($c in $combos) {
    $genLog = "$env:TEMP\diar_gen_$($c.seed).log"
    cmd /c "python scripts\make_synthetic_diar.py --src `"$Src`" --dst `"$Dst`" --num-speakers $($c.n) --turns $($c.turns) --gap-sec $($c.gap) --seed $($c.seed) 2> `"$genLog`""
    if ($LASTEXITCODE -eq 0) {
        $built++
    } else {
        Write-Warning "fixture seed=$($c.seed) n=$($c.n) skipped: $(Get-Content $genLog -Raw)"
    }
}

if ($built -eq 0) {
    Write-Error "no fixtures built — check that $Src holds enough single-speaker wavs"
    exit 2
}
Write-Host "== built $built fixtures; running diar_matrix ($Backend) over them" -ForegroundColor Cyan

# --model only applies to pyannote; speechbrain uses its own (non-gated) default.
# diar_matrix appends the JSON row itself via --evidence, and we redirect its
# stdout/stderr to files (robust: avoids PowerShell swallowing captured output).
$modelArg = if ($Backend -eq "pyannote") { "--model `"$Model`"" } else { "" }
$outFile = "$env:TEMP\diar_matrix_$Backend.out"
$runLog = "$env:TEMP\diar_matrix_$Backend.log"
cmd /c "python scripts\diar_matrix.py --backend $Backend $modelArg --device cuda --audio-dir `"$Dst`" --tag $Tag --evidence `"$EvidenceFile`" 1> `"$outFile`" 2> `"$runLog`""
$code = $LASTEXITCODE

Write-Host "---- diar_matrix progress ----" -ForegroundColor DarkGray
Get-Content $runLog -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }

$json = Get-Content $outFile -Raw -ErrorAction SilentlyContinue
if ($code -ne 0 -or -not $json) {
    Write-Error "diar_matrix failed (exit $code) — see $runLog"
    exit 3
}

Write-Host "`n== DONE. Appended to $EvidenceFile :" -ForegroundColor Green
Write-Host $json.Trim() -ForegroundColor Green
