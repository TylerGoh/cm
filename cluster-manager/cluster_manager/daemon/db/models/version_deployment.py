from datetime import datetime

from sqlalchemy import CheckConstraint, Index, and_, or_
from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime
from cluster_manager.schemas import DeploymentStatus


class VersionDeployment(Base):
    __tablename__ = "version_deployments"

    version: Mapped[str] = mapped_column(primary_key=True)
    upstream_url: Mapped[str]              # where the file server fetches it from, again if it goes missing
    artifact_url: Mapped[str]
    artifact_sha256: Mapped[str]
    status: Mapped[DeploymentStatus]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    activated_by_rollback: Mapped[bool]
    unstable_reason: Mapped[str | None]     # set exactly when UNSTABLE: canary counts, or the rollback's reason


# Constraints are SQLAlchemy expressions over the model's attributes, so a misspelled column or
# enum member fails at import. Built from columns of one table, each attaches to it by itself.
# Enum members render as their names ('ACTIVE'), as the Enum type stores them.

Index(
    "version_deployments_one_active",
    VersionDeployment.status,
    unique=True,
    postgresql_where=VersionDeployment.status == DeploymentStatus.ACTIVE,
)

Index(
    "version_deployments_one_verifying",
    VersionDeployment.status,
    unique=True,
    postgresql_where=VersionDeployment.status == DeploymentStatus.VERIFYING,
)

CheckConstraint(
    or_(
        VersionDeployment.status.not_in([DeploymentStatus.ACTIVE, DeploymentStatus.STABLE]),
        VersionDeployment.verified_at.is_not(None),
    ),
    name="version_deployments_verified_when_promoted",
)

CheckConstraint(
    or_(
        VersionDeployment.status != DeploymentStatus.VERIFYING,
        VersionDeployment.verified_at.is_(None),
    ),
    name="version_deployments_unverified_while_verifying",
)

CheckConstraint(
    or_(
        and_(
            VersionDeployment.status == DeploymentStatus.UNSTABLE,
            VersionDeployment.unstable_reason.is_not(None),
        ),
        and_(
            VersionDeployment.status != DeploymentStatus.UNSTABLE,
            VersionDeployment.unstable_reason.is_(None),
        ),
    ),
    name="version_deployments_reason_when_unstable",
)
