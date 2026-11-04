from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.desired_state import DesiredStateHistoryRow, DesiredStateRow
from cluster_manager.daemon.db.models.version_deployment import VersionDeployment


class DesiredStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, ip: str) -> DesiredStateRow | None:
        return await self._session.get(DesiredStateRow, ip)

    async def get_with_deployment(self, ip: str) -> tuple[DesiredStateRow, VersionDeployment] | None:
        result = await self._session.execute(
            select(DesiredStateRow, VersionDeployment)
            .join(VersionDeployment, DesiredStateRow.version == VersionDeployment.version)
            .where(DesiredStateRow.ip == ip)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    async def apply(
        self,
        ip: str,
        now: datetime,
        *,
        desired_running: bool | None = None,
        version: str | None = None,
        bump_restart: bool = False,
        bump_force_update: bool = False,
    ) -> DesiredStateRow | None:
        values: dict[str, object] = {
            "generation": DesiredStateRow.generation + 1,
            "updated_at": now,
        }
        if desired_running is not None:
            values["desired_running"] = desired_running
        if version is not None:
            values["version"] = version
        if bump_restart:
            values["restart_generation"] = DesiredStateRow.restart_generation + 1
        if bump_force_update:
            values["force_update_generation"] = DesiredStateRow.force_update_generation + 1
        result = await self._session.execute(
            update(DesiredStateRow)
            .where(DesiredStateRow.ip == ip)
            .values(values)
            .returning(DesiredStateRow)
        )
        return result.scalar_one_or_none()

    async def point_at_version(self, ips: list[str], version: str, now: datetime) -> list[DesiredStateRow]:
        """Points every agent in `ips` at `version` in one statement (canary start, reject,
        promote, rollback). Bumps each generation in the database, like apply, and returns
        the new rows so the service can write their history in one bulk insert."""
        if not ips:
            return []
        result = await self._session.execute(
            update(DesiredStateRow)
            .where(DesiredStateRow.ip.in_(ips))
            .values(generation=DesiredStateRow.generation + 1, version=version, updated_at=now)
            .returning(DesiredStateRow)
        )
        return list(result.scalars())

    def add(self, desired: DesiredStateRow) -> None:
        self._session.add(desired)

    def add_history(self, history: DesiredStateHistoryRow) -> None:
        self._session.add(history)

    def add_history_many(self, histories: list[DesiredStateHistoryRow]) -> None:
        self._session.add_all(histories)                 # flushed as one batched insert

    async def history(self, ip: str, limit: int) -> list[DesiredStateHistoryRow]:
        """The agent's most recent desired-state changes, newest first."""
        result = await self._session.execute(
            select(DesiredStateHistoryRow)
            .where(DesiredStateHistoryRow.ip == ip)
            .order_by(DesiredStateHistoryRow.generation.desc())
            .limit(limit)
        )
        return list(result.scalars())
