"""Pure judgements over one agent's rows. No IO, so the views, the canary job and the tests
all get the same answer."""

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta

from cluster_manager.daemon.db.models.desired_state import DesiredStateRow
from cluster_manager.daemon.db.models.observed_state import ObservedState
from cluster_manager.daemon.wire import AgentStatus, ProcessStatus
from cluster_manager.schemas import AgentCondition, CanaryLimits, CanaryOutcome


def agent_condition(desired: DesiredStateRow, observed: ObservedState, *,
                    now: datetime, non_responding_after: timedelta) -> AgentCondition:
    if now - observed.last_heartbeat_at > non_responding_after:
        return AgentCondition.NON_RESPONDING
    if observed.agent_status is AgentStatus.CRASHED_HOLD:
        return AgentCondition.CRASHED_HOLD
    if observed.failed_generation == desired.generation:
        return AgentCondition.FAILED
    if observed.current_operation is not None:
        return AgentCondition.UPDATING
    if observed.acknowledged_generation < desired.generation:
        return AgentCondition.PENDING
    if observed.process_status is ProcessStatus.CRASHED:
        return AgentCondition.CRASHED
    if not desired.desired_running and observed.process_status is ProcessStatus.STOPPED:
        return AgentCondition.STOPPED
    if (desired.desired_running
            and observed.process_status is ProcessStatus.RUNNING
            and observed.running_version == desired.version):
        return AgentCondition.CONVERGED
    return AgentCondition.DRIFTING


def canary_outcome(desired: DesiredStateRow, observed: ObservedState) -> CanaryOutcome:
    """One canary agent's outcome. The canary job uses this at the deadline; the views use it
    to show where the canary stands now."""
    if observed.failed_generation == desired.generation:
        return CanaryOutcome.FAULTY_UPDATE
    if observed.process_status is ProcessStatus.CRASHED:
        return CanaryOutcome.CRASHED
    if observed.process_status is not ProcessStatus.RUNNING or observed.running_version != desired.version:
        return CanaryOutcome.NOT_CONVERGED
    if observed.violations is None:
        return CanaryOutcome.NO_VERDICT
    if observed.violations:
        return CanaryOutcome.PERFORMANCE
    return CanaryOutcome.PASSED


def count_outcomes(outcomes: Iterable[CanaryOutcome]) -> dict[CanaryOutcome, int]:
    """Every outcome present, including zeros, so a reader can line them up."""
    counted = Counter(outcomes)
    return {outcome: counted[outcome] for outcome in CanaryOutcome}


# Outcomes of an agent running the canary version: it converged, whatever its performance.
CONVERGED_OUTCOMES = (CanaryOutcome.NO_VERDICT, CanaryOutcome.PERFORMANCE, CanaryOutcome.PASSED)


def canary_rejected(counts: dict[CanaryOutcome, int], limits: CanaryLimits) -> bool:
    """Any bucket over its limit rejects. NO_VERDICT and PASSED count in no bucket.
    A canary where no agent converged also rejects, whatever the limits say, including one with
    no agents left: passing it would promote the version on no evidence."""
    converged = sum(counts[outcome] for outcome in CONVERGED_OUTCOMES)
    crashes = counts[CanaryOutcome.FAULTY_UPDATE] + counts[CanaryOutcome.CRASHED]
    return (converged == 0
            or crashes > limits.max_crashes
            or counts[CanaryOutcome.PERFORMANCE] > limits.max_violations
            or counts[CanaryOutcome.NOT_CONVERGED] > limits.max_not_converged)
