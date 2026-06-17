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

# (seed, num-speakers, speaker-offset) — distinct files (seed is the unique key),
# mixing 2- and 3-speaker conversations and different CV voices for a fairer DER.
$combos = @(
    @{ seed = 11; n = 2; off = 0 },
    @{ seed = 12; n = 3; off = 0 },
    @{ seed = 13; n = 2; off = 1 },
    @{ seed = 14; n = 3; off = 1 },
    @{ seed = 15; n = 2; off = 2 },
    @{ seed = 16; n = 3; off = 0 }
)

Write-Host "== diar_sweep: backend=$Backend, building $($combos.Count) fixtures into $Dst" -ForegroundColor Cyan

# Fresh fixture dir so n reflects exactly this sweep (old files would inflate n).
if (Test-Path $Dst) { Remove-Item "$Dst\synthetic-diar-*.wav", "$Dst\synthetic-diar-*.rttm" -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Dst | Out-Null

$built = 0
foreach ($c in $combos) {
    $genLog = "$env:TEMP\diar_gen_$($c.seed).log"
    cmd /c "python scripts\make_synthetic_diar.py --src `"$Src`" --dst `"$Dst`" --num-speakers $($c.n) --turns 9 --gap-sec 0.4 --seed $($c.seed) --speaker-offset $($c.off) 2> `"$genLog`""
    if ($LASTEXITCODE -eq 0) {
        $built++
    } else {
        Write-Warning "fixture seed=$($c.seed) n=$($c.n) off=$($c.off) skipped: $(Get-Content $genLog -Raw)"
    }
}

if ($built -eq 0) {
    Write-Error "no fixtures built — check that $Src holds enough single-speaker wavs"
    exit 2
}
Write-Host "== built $built fixtures; running diar_matrix ($Backend) over them" -ForegroundColor Cyan

# Capture stdout (JSON row) only; stderr (per-file DER + summary) goes to a log.
$runLog = "$env:TEMP\diar_matrix_$Backend.log"
$json = cmd /c "python scripts\diar_matrix.py --backend $Backend --model `"$Model`" --device cuda --audio-dir `"$Dst`" --tag $Tag 2> `"$runLog`""

Write-Host "---- diar_matrix stderr (progress) ----" -ForegroundColor DarkGray
Get-Content $runLog | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }

if (-not $json) {
    Write-Error "diar_matrix produced no JSON row — see $runLog"
    exit 3
}

# Append the evidence row (one JSON object per line — same format as wer-results).
$evDir = Split-Path -Parent $EvidenceFile
if (-not (Test-Path $evDir)) { New-Item -ItemType Directory -Force -Path $evDir | Out-Null }
Add-Content -Path $EvidenceFile -Value $json -Encoding utf8

Write-Host "`n== DONE. Appended to $EvidenceFile :" -ForegroundColor Green
Write-Host $json -ForegroundColor Green
