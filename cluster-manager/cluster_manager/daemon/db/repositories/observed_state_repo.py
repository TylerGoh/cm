from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.agent import Agent
from cluster_manager.daemon.db.models.observed_state import ObservedState
from cluster_manager.daemon.wire import ProcessStatus


class ObservedStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, ip: str) -> ObservedState | None:
        return await self._session.get(ObservedState, ip)

    async def save(self, observed: ObservedState) -> ObservedState:
        return await self._session.merge(observed)

    async def silent_ips(self, now: datetime, threshold: timedelta) -> list[str]:
        """Live agents last heard from before `now - threshold`. The cutoff is computed here,
        not in SQL: SQLite has no interval arithmetic."""
        cutoff = now - threshold
        result = await self._session.execute(
            select(ObservedState.ip)
            .join(Agent, Agent.ip == ObservedState.ip)
            .where(
                Agent.decommissioned_at.is_(None),
                ObservedState.last_heartbeat_at < cutoff,
            )
        )
        return list(result.scalars())

    async def canary_sample(self, size: int, now: datetime, threshold: timedelta) -> list[str]:
        """Up to `size` random eligible agents for a canary: live (not decommissioned, heard from
        since `now - threshold`) and last reported RUNNING. One query, whatever the fleet size."""
        cutoff = now - threshold
        result = await self._session.execute(
            select(ObservedState.ip)
            .join(Agent, Agent.ip == ObservedState.ip)
            .where(
                Agent.decommissioned_at.is_(None),
                ObservedState.last_heartbeat_at >= cutoff,
                ObservedState.process_status == ProcessStatus.RUNNING,
            )
            .order_by(func.random())
            .limit(size)
        )
        return list(result.scalars())
