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
