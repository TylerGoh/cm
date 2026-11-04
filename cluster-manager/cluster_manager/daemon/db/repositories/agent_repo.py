from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.agent import Agent
from cluster_manager.daemon.db.models.desired_state import DesiredStateRow
from cluster_manager.daemon.db.models.observed_state import ObservedState

type AgentRows = tuple[Agent, DesiredStateRow, ObservedState]


class AgentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, ip: str) -> Agent | None:
        return await self._session.get(Agent, ip)

    def add(self, agent: Agent) -> None:
        self._session.add(agent)

    async def list_live(self, version: str | None = None) -> list[AgentRows]:
        """Every live agent with its desired and observed rows, in one join. Registration writes
        all three in one transaction, so every agent has both."""
        statement = (
            select(Agent, DesiredStateRow, ObservedState)
            .join(DesiredStateRow, DesiredStateRow.ip == Agent.ip)
            .join(ObservedState, ObservedState.ip == Agent.ip)
            .where(Agent.decommissioned_at.is_(None))
            .order_by(Agent.ip)
        )
        if version is not None:
            statement = statement.where(DesiredStateRow.version == version)
        result = await self._session.execute(statement)
        return [(agent, desired, observed) for agent, desired, observed in result.all()]

    async def get_with_state(self, ip: str) -> AgentRows | None:
        """One agent with its rows, decommissioned or not."""
        result = await self._session.execute(
            select(Agent, DesiredStateRow, ObservedState)
            .join(DesiredStateRow, DesiredStateRow.ip == Agent.ip)
            .join(ObservedState, ObservedState.ip == Agent.ip)
            .where(Agent.ip == ip)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return row[0], row[1], row[2]
