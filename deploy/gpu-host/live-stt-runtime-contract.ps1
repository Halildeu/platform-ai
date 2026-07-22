# Source-controlled production contract shared by the live-STT launcher and deploy acceptance.
# Values are public runtime policy, never credentials.

$script:LiveSttModelLoadTimeoutSec = 180
$script:LiveSttWorkerKillGraceSec = 2
$script:LiveSttPreloadMaxAttempts = 2
$script:LiveSttPreloadRetryBaseSec = 1
$script:LiveSttPreloadRoleCount = 2
$script:LiveSttSmokeWorstCaseSec = 150
$script:LiveSttTaskTransitionReserveSec = 60
$script:LiveSttReadinessDeadlineSec = 960

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
    $script:LiveSttTaskTransitionReserveSec
)

if ($script:LiveSttAcceptanceWorstCaseSec -gt $script:LiveSttReadinessDeadlineSec) {
    throw "Live STT end-to-end acceptance contract exceeds its readiness deadline."
}
