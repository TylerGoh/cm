"""Read views for the client. Each function builds one response model from the repositories.
Reads only: nothing here commits. Every judgement comes from services/conditions.py."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.models.agent import Agent
from cluster_manager.daemon.db.models.desired_state import DesiredStateHistoryRow, DesiredStateRow
from cluster_manager.daemon.db.models.incident import Incident
from cluster_manager.daemon.db.models.observed_state import ObservedState
from cluster_manager.daemon.db.models.operation import Operation
from cluster_manager.daemon.db.repositories.agent_repo import AgentRepo
from cluster_manager.daemon.db.repositories.desired_state_repo import DesiredStateRepo
from cluster_manager.daemon.db.repositories.incident_repo import IncidentRepo
from cluster_manager.daemon.db.repositories.operation_repo import OperationRepo
from cluster_manager.daemon.db.repositories.version_deployment_repo import VersionDeploymentRepo
from cluster_manager.daemon.services.errors import AgentNotFound
from cluster_manager.daemon.services.conditions import (
    agent_condition, canary_outcome, canary_rejected, count_outcomes,
)
from cluster_manager.daemon.wire import OperationView
from cluster_manager.schemas import (
    AgentCondition, AgentDetailResponse, AgentListResponse, AgentRow, CanaryAgent, CanaryLimits,
    CanaryResponse, CanarySummary, CanaryView, DesiredChange, IncidentListResponse, IncidentRow,
    StatusResponse, VersionListResponse, VersionRow,
)

OPERATIONS_SHOWN = 10
HISTORY_SHOWN = 10
CLOSED_INCIDENTS_SHOWN = 20

# Conditions that need nobody's attention; everything else is a problem.
SETTLED = (AgentCondition.CONVERGED, AgentCondition.STOPPED)


# --- row builders ---

def agent_row(desired: DesiredStateRow, observed: ObservedState, now: datetime) -> AgentRow:
    current = observed.current_operation
    return AgentRow(
        ip=desired.ip,
        condition=agent_condition(desired, observed, now=now,
                                  non_responding_after=config.non_responding_after),
        desired_version=desired.version,
        running_version=observed.running_version,
        desired_running=desired.desired_running,
        process_status=observed.process_status,
        agent_status=observed.agent_status,
        healthy=observed.healthy,
        violations=observed.violations,
        metrics_available=observed.metrics_available,
        last_heartbeat_at=observed.last_heartbeat_at,
        current_operation=OperationView.model_validate(current).type if current is not None else None,
    )


def desired_change(history: DesiredStateHistoryRow) -> DesiredChange:
    return DesiredChange(
        generation=history.generation,
        version=history.version,
        desired_running=history.desired_running,
        restart_generation=history.restart_generation,
        force_update_generation=history.force_update_generation,
        changed_at=history.changed_at,
    )


def incident_row(incident: Incident) -> IncidentRow:
    return IncidentRow(
        id=incident.id,
        agent_ip=incident.agent_ip,
        kind=incident.kind,
        opened_at=incident.opened_at,
        closed_at=incident.closed_at,
        notified=incident.notified_at is not None,
        recovery_notified=incident.recovery_notified_at is not None,
    )


def operation_view(operation: Operation) -> OperationView:
    return OperationView.model_validate({
        "op_id": operation.op_id,
        "generation": operation.generation,
        "type": operation.type,
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
        "steps": operation.steps,
        "failure": operation.failure,
    })


def canary_limits() -> CanaryLimits:
    return CanaryLimits(
        max_crashes=config.canary_max_crashes,
        max_violations=config.canary_max_violations,
        max_not_converged=config.canary_max_not_converged,
    )


# --- views ---

async def status(session: AsyncSession) -> StatusResponse:
    now = datetime.now(UTC)
    versions = VersionDeploymentRepo(session)
    active = await versions.active()
    verifying = await versions.verifying()
    rows = await AgentRepo(session).list_live()

    conditions: dict[AgentCondition, int] = {}
    for _, desired, observed in rows:
        condition = agent_row(desired, observed, now).condition
        conditions[condition] = conditions.get(condition, 0) + 1

    canary: CanarySummary | None = None
    if verifying is not None:
        outcomes = [canary_outcome(desired, observed)
                    for _, desired, observed in rows if desired.version == verifying.version]
        canary = CanarySummary(
            version=verifying.version,
            deadline=verifying.created_at + config.canary_duration,
            agents=len(outcomes),
            counts=count_outcomes(outcomes),
        )

    return StatusResponse(
        active_version=active.version,
        verifying_version=verifying.version if verifying is not None else None,
        canary=canary,
        agents=len(rows),
        conditions=conditions,
        open_incidents=len(await IncidentRepo(session).open_incidents()),
    )


async def agents(session: AsyncSession, *, condition: AgentCondition | None,
                 version: str | None, problems: bool) -> AgentListResponse:
    now = datetime.now(UTC)
    result: list[AgentRow] = []
    for _, desired, observed in await AgentRepo(session).list_live(version):
        row = agent_row(desired, observed, now)
        if condition is not None and row.condition is not condition:
            continue
        if problems and row.condition in SETTLED:
            continue
        result.append(row)
    return AgentListResponse(agents=result)


async def agent_detail(session: AsyncSession, ip: str) -> AgentDetailResponse:
    found = await AgentRepo(session).get_with_state(ip)
    if found is None:
        raise AgentNotFound(ip)
    agent, desired, observed = found
    return _detail(agent, desired, observed,
                   operations=await OperationRepo(session).recent(ip, OPERATIONS_SHOWN),
                   history=await DesiredStateRepo(session).history(ip, HISTORY_SHOWN),
                   incidents=await IncidentRepo(session).open_for_agent(ip))


def _detail(agent: Agent, desired: DesiredStateRow, observed: ObservedState, *,
            operations: list[Operation], history: list[DesiredStateHistoryRow],
            incidents: list[Incident]) -> AgentDetailResponse:
    current = observed.current_operation
    last = observed.last_operation
    return AgentDetailResponse(
        agent=agent_row(desired, observed, datetime.now(UTC)),
        registered_at=agent.registered_at,
        generation=desired.generation,
        acknowledged_generation=observed.acknowledged_generation,
        failed_generation=observed.failed_generation,
        restart_generation=desired.restart_generation,
        handled_restart_generation=observed.handled_restart_generation,
        force_update_generation=desired.force_update_generation,
        handled_force_update_generation=observed.handled_force_update_generation,
        current_operation=OperationView.model_validate(current) if current is not None else None,
        last_operation=OperationView.model_validate(last) if last is not None else None,
        recent_operations=[operation_view(op) for op in operations],
        history=[desired_change(h) for h in history],
        open_incidents=[incident_row(i) for i in incidents],
    )


async def versions(session: AsyncSession) -> VersionListResponse:
    agents_per_version: dict[str, int] = {}
    for _, desired, _ in await AgentRepo(session).list_live():
        agents_per_version[desired.version] = agents_per_version.get(desired.version, 0) + 1
    return VersionListResponse(versions=[
        VersionRow(
            version=v.version,
            status=v.status,
            created_at=v.created_at,
            verified_at=v.verified_at,
            activated_by_rollback=v.activated_by_rollback,
            unstable_reason=v.unstable_reason,
            agents=agents_per_version.get(v.version, 0),
        )
        for v in await VersionDeploymentRepo(session).all()
    ])


async def canary(session: AsyncSession) -> CanaryResponse:
    verifying = await VersionDeploymentRepo(session).verifying()
    if verifying is None:
        return CanaryResponse(canary=None)
    canary_agents = [
        CanaryAgent(ip=desired.ip, outcome=canary_outcome(desired, observed))
        for _, desired, observed in await AgentRepo(session).list_live(verifying.version)
    ]
    counts = count_outcomes(a.outcome for a in canary_agents)
    limits = canary_limits()
    return CanaryResponse(canary=CanaryView(
        version=verifying.version,
        created_at=verifying.created_at,
        deadline=verifying.created_at + config.canary_duration,
        agents=canary_agents,
        counts=counts,
        limits=limits,
        would_reject=canary_rejected(counts, limits),
    ))


async def incidents(session: AsyncSession) -> IncidentListResponse:
    repo = IncidentRepo(session)
    return IncidentListResponse(
        open=[incident_row(i) for i in await repo.open_incidents()],
        recently_closed=[incident_row(i) for i in await repo.recently_closed(CLOSED_INCIDENTS_SHOWN)],
    )
