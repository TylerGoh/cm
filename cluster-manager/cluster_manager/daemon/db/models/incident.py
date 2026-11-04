from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, and_, or_
from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime
from cluster_manager.schemas import IncidentKind


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_ip: Mapped[str] = mapped_column(ForeignKey("agents.ip"))
    kind: Mapped[IncidentKind]
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    recovery_notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


# Constraints are SQLAlchemy expressions over the model's attributes (see version_deployment.py).

Index(
    "incidents_one_open",
    Incident.agent_ip,
    Incident.kind,
    unique=True,
    postgresql_where=Incident.closed_at.is_(None),
    sqlite_where=Incident.closed_at.is_(None),
)

CheckConstraint(
    or_(
        Incident.recovery_notified_at.is_(None),
        and_(Incident.closed_at.is_not(None), Incident.notified_at.is_not(None)),
    ),
    name="incidents_recovery_after_opening",
)
