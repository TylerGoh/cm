"""The heartbeat, in one transaction: register an unknown IP, refuse a decommissioned one, write
the report, record the last operation, close a non_responding incident, open or close the
metrics_unavailable incident, and build the reply. The reply is built from the desired state
alone, never from observed state."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.clients.file_server import FileServerClient
from cluster_manager.daemon.db.models.agent import Agent
from cluster_manager.daemon.db.models.desired_state import DesiredStateRow
from cluster_manager.daemon.db.models.observed_state import ObservedState
from cluster_manager.daemon.db.models.operation import Operation
from cluster_manager.daemon.db.repositories.agent_repo import AgentRepo
from cluster_manager.daemon.db.repositories.desired_state_repo import DesiredStateRepo
from cluster_manager.daemon.db.repositories.incident_repo import IncidentRepo
from cluster_manager.daemon.db.repositories.observed_state_repo import ObservedStateRepo
from cluster_manager.daemon.db.repositories.operation_repo import OperationRepo
from cluster_manager.daemon.db.repositories.version_deployment_repo import VersionDeploymentRepo
from cluster_manager.daemon.services.desired_state import history_row
from cluster_manager.daemon.wire import DesiredState, HeartbeatReport
from cluster_manager.schemas import IncidentKind, IncidentSource


class AgentDecommissioned(Exception):
    """A heartbeat from a decommissioned IP. The agent sees an error, as for any failed heartbeat."""


async def handle(session: AsyncSession, report: HeartbeatReport, file_server: FileServerClient,
                 now: datetime) -> DesiredState:
    ip = str(report.ipv4)                            # canonical form, once, where it reaches the database
    agent = await AgentRepo(session).get(ip)
    if agent is None:
        await _register(session, ip, report, now)
    elif agent.decommissioned_at is not None:
        raise AgentDecommissioned(ip)

    await ObservedStateRepo(session).save(_observed(ip, report, now))
    if report.last_operation is not None:
        await OperationRepo(session).record(_operation(ip, report))

    incidents = IncidentRepo(session)
    agent = IncidentSource.AGENT
    await incidents.close_open(ip, IncidentKind.NON_RESPONDING, now, source=agent)
    if report.metrics_available is False:
        await incidents.open(ip, IncidentKind.METRICS_UNAVAILABLE, now, source=agent)
    elif report.metrics_available is True:
        await incidents.close_open(ip, IncidentKind.METRICS_UNAVAILABLE, now, source=agent)
    # None neither opens nor closes it: nobody saw the metrics recover.

    found = await DesiredStateRepo(session).get_with_deployment(ip)
    assert found is not None                         # registration writes the row in this transaction
    desired, deployment = found
    hypertree_url = file_server.hypertree_url(desired.version)
    reply = DesiredState(
        generation=desired.generation,
        restart_generation=desired.restart_generation,
        force_update_generation=desired.force_update_generation,
        desired_running=desired.desired_running,
        version=desired.version,
        artifact_url=deployment.artifact_url,
        artifact_sha256=deployment.artifact_sha256,
        hypertree_get_url=hypertree_url,
        hypertree_upload_url=hypertree_url,
    )
    await session.commit()
    return reply


async def _register(session: AsyncSession, ip: str, report: HeartbeatReport, now: datetime) -> None:
    """The first heartbeat from an unknown IP. The desired state is seeded from the report, so a
    re-registered agent never gets a generation it would reject as stale, and no counter bump is
    skipped. A new agent runs the ACTIVE version (startup guarantees there is exactly one)."""
    active = await VersionDeploymentRepo(session).active()
    AgentRepo(session).add(Agent(ip=ip, registered_at=now, decommissioned_at=None))
    desired = DesiredStateRow(
        ip=ip,
        generation=report.acknowledged_generation + 1,
        restart_generation=report.handled_restart_generation,
        force_update_generation=report.handled_force_update_generation,
        desired_running=True,
        version=active.version,
        updated_at=now,
    )
    repo = DesiredStateRepo(session)
    repo.add(desired)
    await session.flush()                            # the agent and its desired row before anything refers to them
    repo.add_history(history_row(desired))


def _observed(ip: str, report: HeartbeatReport, now: datetime) -> ObservedState:
    return ObservedState(
        ip=ip,
        last_heartbeat_at=now,
        acknowledged_generation=report.acknowledged_generation,
        handled_restart_generation=report.handled_restart_generation,
        handled_force_update_generation=report.handled_force_update_generation,
        failed_generation=report.failed_generation,
        running_version=report.running_version,
        process_status=report.process_status,
        healthy=report.healthy,
        violations=report.violations,
        metrics_available=report.metrics_available,
        agent_status=report.agent_status,
        current_operation=(report.current_operation.model_dump(mode="json")
                           if report.current_operation is not None else None),
        last_operation=(report.last_operation.model_dump(mode="json")
                        if report.last_operation is not None else None),
    )


def _operation(ip: str, report: HeartbeatReport) -> Operation:
    view = report.last_operation
    assert view is not None and view.finished_at is not None
    return Operation(
        agent_ip=ip,
        op_id=view.op_id,
        generation=view.generation,
        type=view.type,
        started_at=view.started_at,
        finished_at=view.finished_at,
        failure=view.failure,
        steps=[step.model_dump(mode="json") for step in view.steps],
    )
