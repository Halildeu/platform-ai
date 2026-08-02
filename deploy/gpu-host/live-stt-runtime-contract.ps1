# Source-controlled production contract shared by the live-STT launcher and deploy acceptance.
# Values are public runtime policy, never credentials.

# Measured on the GPU host (RTX 4070 Laptop, 8 GiB) loading the pinned
# large-v3-turbo final model (1.54 GiB model.bin) into a spawned worker:
#
#   2026-07-26 19:38:14  role=final preloaded  elapsed_sec=160.6
#   2026-07-26 21:03:52  role=final preloaded  elapsed_sec=167.9
#   2026-07-26 21:30:47  role=final FAILED     elapsed_sec=180.1  WorkerTimeoutError
#
# The previous 180s budget sat ~7% above the observed load time, so ordinary
# variance (VRAM contention from the already-resident live model, CUDA context
# init, cold page cache) crossed it. A preload timeout is not a soft failure
# here: fail-closed readiness means the deploy is rejected, and a rejected
# deploy whose rollback also fails leaves the runtime fenced with both tasks
# disabled. The budget is therefore set to ~2x the observed maximum: a too-large
# ceiling only delays detecting a genuinely stuck load, while a too-small one
# takes the service down.
$script:LiveSttModelLoadTimeoutSec = 360
$script:LiveSttWorkerKillGraceSec = 2
$script:LiveSttPreloadMaxAttempts = 2
$script:LiveSttPreloadRetryBaseSec = 1
$script:LiveSttPreloadRoleCount = 2
$script:LiveSttSmokeWorstCaseSec = 150
$script:LiveSttTaskTransitionReserveSec = 60
# 30s was measured too tight on the combined restart path (2026-08-02, Denetim
# host): the acceptance restarts live-stt seconds before polling meeting-ai
# /ready, so the FastAPI cold start races Whisper preload for GPU/disk and the
# forward AND rollback acceptances both failed meeting-ai-readiness while a solo
# meeting-ai restart answered /ready in ~15s. 120s absorbs that contention and
# still fits under the 1800s ceiling (guard below).
$script:MeetingAiReadinessDeadlineSec = 120
# Derived ceiling, not a free parameter: it must stay above
# LiveSttAcceptanceWorstCaseSec, which the guard at the bottom of this file
# enforces. With a 360s model-load budget the worst case is
#   2 roles * ((2 attempts * (360 + 2*2)) + 1 retry backoff) + 150 + 60 + 120 = 1788.
$script:LiveSttReadinessDeadlineSec = 1800

# Production speech-gate profile. RMS stays deliberately low so quiet speech
# reaches the decoder; pinned Silero VAD parameters reject pause/noise without
# depending on faster-whisper library defaults. Only the RMS pair may be
# overridden by the hardened ProgramData config.
$script:LiveSttSpeechGateProfile = "silero-balanced-v1"
$script:LiveSttSilenceRms = "0.0005"
$script:LiveSttMinSpeechRms = "0.0005"
$script:LiveSttStreamVadThreshold = "0.35"
$script:LiveSttStreamVadMinSpeechDurationMs = 100
$script:LiveSttStreamVadMinSilenceDurationMs = 300
$script:LiveSttStreamVadSpeechPadMs = 100
$script:LiveSttLiveInferIntervalMs = 700
$script:LiveSttLiveWindowSec = "2.0"
$script:LiveSttFinalWindowSec = "6.0"
$script:LiveSttForcedCommitSec = "5.0"
$script:LiveSttSilenceCommitSec = "0.7"
$script:LiveSttTailOverlapSec = "0.25"
$script:LiveSttMinInferSec = "0.35"
$script:LiveSttContextualArtifactMaxRms = "0.02"
$script:LiveSttContextualArtifactMinNoSpeechProb = "0.6"

$script:LiveSttPreloadRetryWorstCaseSec = $script:LiveSttPreloadRetryBaseSec * (
    [Math]::Pow(2, $script:LiveSttPreloadMaxAttempts - 1) - 1
)
$script:LiveSttPreloadWorstCaseSec = $script:LiveSttPreloadRoleCount * (
    ($script:LiveSttPreloadMaxAttempts * (
        $script:LiveSttModelLoadTimeoutSec + (2 * $script:LiveSttWorkerKillGraceSec)
    )) +
    $script:LiveSttPreloadRetryWorstCaseSec
)

$script:LiveSttAcceptanceWorstCaseSec = (
    $script:LiveSttPreloadWorstCaseSec +
    $script:LiveSttSmokeWorstCaseSec +
    $script:LiveSttTaskTransitionReserveSec +
    $script:MeetingAiReadinessDeadlineSec
)

if ($script:LiveSttAcceptanceWorstCaseSec -gt $script:LiveSttReadinessDeadlineSec) {
    throw "Live STT end-to-end acceptance contract exceeds its readiness deadline."
}
