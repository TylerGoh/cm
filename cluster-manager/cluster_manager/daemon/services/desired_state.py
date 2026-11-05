"""Per-agent mutators. Each one changes the desired state through DesiredStateRepo.apply (which
bumps the generation), writes the history row, and commits. A refusal changes nothing."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.desired_state import DesiredStateHistoryRow, DesiredStateRow
from cluster_manager.daemon.db.models.observed_state import ObservedState
from cluster_manager.daemon.db.repositories.agent_repo import AgentRepo
from cluster_manager.daemon.db.repositories.desired_state_repo import DesiredStateRepo
from cluster_manager.daemon.services.errors import ActionRefused, AgentNotFound
from cluster_manager.daemon.services.views import desired_change
from cluster_manager.daemon.wire import ProcessStatus
from cluster_manager.schemas import DecommissionResponse, DesiredStateChangeResponse


def history_row(row: DesiredStateRow) -> DesiredStateHistoryRow:
    return DesiredStateHistoryRow(
        ip=row.ip,
        generation=row.generation,
        restart_generation=row.restart_generation,
        force_update_generation=row.force_update_generation,
        desired_running=row.desired_running,
        version=row.version,
        changed_at=row.updated_at,
    )


async def _live_rows(session: AsyncSession, ip: str) -> tuple[DesiredStateRow, ObservedState]:
    found = await AgentRepo(session).get_with_state(ip)
    if found is None:
        raise AgentNotFound(ip)
    agent, desired, observed = found
    if agent.decommissioned_at is not None:
        raise ActionRefused(f"{ip} is decommissioned")
    return desired, observed


async def _commit_change(session: AsyncSession, row: DesiredStateRow | None,
                         ip: str) -> DesiredStateChangeResponse:
    if row is None:
        raise AgentNotFound(ip)
    history = history_row(row)
    DesiredStateRepo(session).add_history(history)
    await session.commit()
    return DesiredStateChangeResponse(ip=ip, desired=desired_change(history))


async def restart(session: AsyncSession, ip: str) -> DesiredStateChangeResponse:
    """Bumps restart_generation. The agent runs RESTART only from RUNNING; otherwise the bump
    waits until the worker runs again."""
    await _live_rows(session, ip)
    row = await DesiredStateRepo(session).apply(ip, datetime.now(UTC), bump_restart=True)
    return await _commit_change(session, row, ip)


async def force_update(session: AsyncSession, ip: str) -> DesiredStateChangeResponse:
    """Bumps force_update_generation, only while the worker is CRASHED or STOPPED: the agent runs
    FORCE_UPDATE from those two, and a bump while RUNNING would wait for the next crash."""
    _, observed = await _live_rows(session, ip)
    if observed.process_status not in (ProcessStatus.CRASHED, ProcessStatus.STOPPED):
        raise ActionRefused(f"{ip} is {observed.process_status}; force updates are for crashed or stopped "
                            f"workers (use restart for a running one)")
    row = await DesiredStateRepo(session).apply(ip, datetime.now(UTC), bump_force_update=True)
    return await _commit_change(session, row, ip)


async def set_running(session: AsyncSession, ip: str, running: bool) -> DesiredStateChangeResponse:
    desired, _ = await _live_rows(session, ip)
    if desired.desired_running is running:
        raise ActionRefused(f"{ip} already has desired_running={running}")
    row = await DesiredStateRepo(session).apply(ip, datetime.now(UTC), desired_running=running)
    return await _commit_change(session, row, ip)


async def decommission(session: AsyncSession, ip: str) -> DecommissionResponse:
    """Marks the agent decommissioned. Its heartbeats are refused from now on, and it drops out
    of every list. Its desired state is left as it is."""
    agent = await AgentRepo(session).get(ip)
    if agent is None:
        raise AgentNotFound(ip)
    if agent.decommissioned_at is not None:
        raise ActionRefused(f"{ip} was already decommissioned at {agent.decommissioned_at}")
    agent.decommissioned_at = datetime.now(UTC)
    await session.commit()
    return DecommissionResponse(ip=ip, decommissioned_at=agent.decommissioned_at)
