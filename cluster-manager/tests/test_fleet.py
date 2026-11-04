"""The ACTIVE fleet's judgement: which agents count, and when the fleet fails. Pure, no database."""

from datetime import UTC, datetime, timedelta

from cluster_manager.daemon.db.models.desired_state import DesiredStateRow
from cluster_manager.daemon.db.models.observed_state import ObservedState
from cluster_manager.daemon.services.conditions import FleetLimits, count_outcomes, fleet_failing, fleet_outcome
from cluster_manager.daemon.wire import AgentStatus, ProcessStatus
from cluster_manager.schemas import CanaryOutcome

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
THRESHOLD = timedelta(seconds=30)
LIMITS = FleetLimits(max_crashes=2, max_violations=2, max_not_converged=2)


def desired(*, running: bool = True, generation: int = 4) -> DesiredStateRow:
    return DesiredStateRow(ip="10.0.0.1", generation=generation, restart_generation=0,
                           force_update_generation=0, desired_running=running, version="job_2",
                           updated_at=NOW)


def observed(*, status: ProcessStatus = ProcessStatus.RUNNING, version: str | None = "job_2",
             acknowledged: int = 4, failed: int | None = None, violations: list[str] | None = [],
             operation: dict[str, object] | None = None, seen: datetime = NOW) -> ObservedState:
    return ObservedState(ip="10.0.0.1", last_heartbeat_at=seen, acknowledged_generation=acknowledged,
                         handled_restart_generation=0, handled_force_update_generation=0,
                         failed_generation=failed, running_version=version, process_status=status,
                         healthy=True, violations=violations, metrics_available=True,
                         agent_status=AgentStatus.IDLE, current_operation=operation, last_operation=None)


def outcome(d: DesiredStateRow, o: ObservedState) -> CanaryOutcome | None:
    return fleet_outcome(d, o, now=NOW, non_responding_after=THRESHOLD)


def test_a_settled_agent_gets_the_canary_outcome() -> None:
    assert outcome(desired(), observed()) is CanaryOutcome.PASSED
    assert outcome(desired(), observed(status=ProcessStatus.CRASHED, version=None)) is CanaryOutcome.CRASHED
    assert outcome(desired(), observed(failed=4)) is CanaryOutcome.FAULTY_UPDATE
    assert outcome(desired(), observed(violations=["too many failed"])) is CanaryOutcome.PERFORMANCE


def test_settling_agents_dont_count() -> None:
    assert outcome(desired(generation=5), observed(acknowledged=4, version="job_1")) is None    # not acknowledged
    assert outcome(desired(), observed(version="job_1", operation={"type": "update"})) is None # updating


def test_silent_and_stopped_agents_dont_count() -> None:
    assert outcome(desired(), observed(seen=NOW - timedelta(minutes=5))) is None
    assert outcome(desired(running=False), observed(status=ProcessStatus.STOPPED, version=None)) is None


def test_the_fleet_fails_only_over_a_limit() -> None:
    three_crashed = count_outcomes([CanaryOutcome.CRASHED] * 3 + [CanaryOutcome.PASSED] * 10)
    two_crashed = count_outcomes([CanaryOutcome.CRASHED, CanaryOutcome.FAULTY_UPDATE] + [CanaryOutcome.PASSED] * 10)
    assert fleet_failing(three_crashed, LIMITS)
    assert not fleet_failing(two_crashed, LIMITS)


def test_a_fleet_with_nothing_to_count_is_not_failing() -> None:
    assert not fleet_failing(count_outcomes([]), LIMITS)        # everyone settling after a promotion
