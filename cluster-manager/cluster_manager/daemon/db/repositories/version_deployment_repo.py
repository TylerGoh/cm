from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.version_deployment import VersionDeployment
from cluster_manager.schemas import DeploymentStatus


class VersionDeploymentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, version: str) -> VersionDeployment | None:
        return await self._session.get(VersionDeployment, version)

    async def has_any(self) -> bool:
        """Whether any version was ever recorded. False only on the daemon's first boot."""
        result = await self._session.execute(select(VersionDeployment.version).limit(1))
        return result.scalar_one_or_none() is not None

    async def active(self) -> VersionDeployment:
        result = await self._session.execute(
            select(VersionDeployment).where(VersionDeployment.status == DeploymentStatus.ACTIVE)
        )
        return result.scalar_one()

    async def verifying(self) -> VersionDeployment | None:
        result = await self._session.execute(
            select(VersionDeployment).where(VersionDeployment.status == DeploymentStatus.VERIFYING)
        )
        return result.scalar_one_or_none()

    async def latest_stable(self) -> VersionDeployment | None:
        """The rollback target: the STABLE version verified most recently."""
        result = await self._session.execute(
            select(VersionDeployment)
            .where(VersionDeployment.status == DeploymentStatus.STABLE)
            .order_by(VersionDeployment.verified_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def all(self) -> list[VersionDeployment]:
        """Every version, newest first."""
        result = await self._session.execute(
            select(VersionDeployment).order_by(VersionDeployment.created_at.desc())
        )
        return list(result.scalars())

    async def not_unstable(self) -> list[VersionDeployment]:
        """The versions agents may still be pointed at: every one that isn't UNSTABLE."""
        result = await self._session.execute(
            select(VersionDeployment).where(VersionDeployment.status != DeploymentStatus.UNSTABLE)
        )
        return list(result.scalars())

    def add(self, deployment: VersionDeployment) -> None:
        self._session.add(deployment)
