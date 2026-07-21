# Source-controlled production contract shared by the live-STT launcher and deploy acceptance.
# Values are public runtime policy, never credentials.

$script:LiveSttModelLoadTimeoutSec = 180
$script:LiveSttWorkerKillGraceSec = 2
$script:LiveSttPreloadMaxAttempts = 2
$script:LiveSttPreloadRetryBaseSec = 1
$script:LiveSttPreloadRoleCount = 2
$script:LiveSttReadinessDeadlineSec = 780

$script:LiveSttPreloadWorstCaseSec = $script:LiveSttPreloadRoleCount * (
    ($script:LiveSttPreloadMaxAttempts * (
        $script:LiveSttModelLoadTimeoutSec + (2 * $script:LiveSttWorkerKillGraceSec)
    )) +
    (($script:LiveSttPreloadMaxAttempts - 1) * $script:LiveSttPreloadRetryBaseSec)
)

if ($script:LiveSttPreloadWorstCaseSec -gt $script:LiveSttReadinessDeadlineSec) {
    throw "Live STT preload contract exceeds its readiness deadline."
}
