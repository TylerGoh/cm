from datetime import datetime

from sqlalchemy import CheckConstraint, Index, and_, or_
from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime
from cluster_manager.schemas import IncidentKind, IncidentSource


class Incident(Base):
    """Information only, with no foreign key: an incident stays as it was recorded, whatever
    happens to the agent or version it names."""
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[IncidentSource]
    ip: Mapped[str]                         # the agent's, or the cluster manager's own IPV4
    version: Mapped[str | None]             # the version it is about, for a cluster manager incident
    kind: Mapped[IncidentKind]
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    recovery_notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


# NULLS NOT DISTINCT: an agent incident has no version, and two of them must still collide.
Index(
    "incidents_one_open",
    Incident.source,
    Incident.ip,
    Incident.version,
    Incident.kind,
    unique=True,
    postgresql_where=Incident.closed_at.is_(None),
    postgresql_nulls_not_distinct=True,
)

CheckConstraint(
    or_(
        Incident.recovery_notified_at.is_(None),
        and_(Incident.closed_at.is_not(None), Incident.notified_at.is_not(None)),
    ),
    name="incidents_recovery_after_opening",
)
