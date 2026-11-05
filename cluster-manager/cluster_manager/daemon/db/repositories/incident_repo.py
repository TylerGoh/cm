from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.incident import Incident
from cluster_manager.schemas import IncidentKind


class IncidentRepo:
    """Every timestamp is the `now` the caller passes in: the daemon's clock, never func.now()."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open(self, agent_ip: str, kind: IncidentKind, now: datetime) -> Incident | None:
        result = await self._session.execute(
            insert(Incident)
            .values(
                agent_ip=agent_ip,
                kind=kind,
                opened_at=now,
                closed_at=None,
                notified_at=None,
                recovery_notified_at=None,
            )
            .on_conflict_do_nothing(
                index_elements=[Incident.agent_ip, Incident.kind],
                index_where=Incident.closed_at.is_(None),
            )
            .returning(Incident)
        )
        return result.scalar_one_or_none()

    async def close_open(self, agent_ip: str, kind: IncidentKind, now: datetime) -> Incident | None:
        result = await self._session.execute(
            update(Incident)
            .where(Incident.agent_ip == agent_ip, Incident.kind == kind, Incident.closed_at.is_(None))
            .values(closed_at=now)
            .returning(Incident)
        )
        return result.scalar_one_or_none()

    async def open_incidents(self) -> list[Incident]:
        result = await self._session.execute(
            select(Incident).where(Incident.closed_at.is_(None)).order_by(Incident.opened_at)
        )
        return list(result.scalars())

    async def open_for_agent(self, agent_ip: str) -> list[Incident]:
        result = await self._session.execute(
            select(Incident)
            .where(Incident.agent_ip == agent_ip, Incident.closed_at.is_(None))
            .order_by(Incident.opened_at)
        )
        return list(result.scalars())

    async def recently_closed(self, limit: int) -> list[Incident]:
        """The most recently closed incidents, newest first."""
        result = await self._session.execute(
            select(Incident)
            .where(Incident.closed_at.is_not(None))
            .order_by(Incident.closed_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def pending_openings(self) -> list[Incident]:
        result = await self._session.execute(
            select(Incident).where(Incident.notified_at.is_(None)).order_by(Incident.opened_at)
        )
        return list(result.scalars())

    async def pending_recoveries(self) -> list[Incident]:
        result = await self._session.execute(
            select(Incident)
            .where(
                Incident.closed_at.is_not(None),
                Incident.notified_at.is_not(None),
                Incident.recovery_notified_at.is_(None),
            )
            .order_by(Incident.closed_at)
        )
        return list(result.scalars())

    async def mark_opening_sent(self, incident_id: int, now: datetime) -> None:
        await self._session.execute(
            update(Incident).where(Incident.id == incident_id).values(notified_at=now)
        )

    async def mark_recovery_sent(self, incident_id: int, now: datetime) -> None:
        await self._session.execute(
            update(Incident).where(Incident.id == incident_id).values(recovery_notified_at=now)
        )
