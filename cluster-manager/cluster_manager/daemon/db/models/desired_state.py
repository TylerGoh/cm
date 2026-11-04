from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime


class DesiredStateRow(Base):
    __tablename__ = "desired_state"

    ip: Mapped[str] = mapped_column(ForeignKey("agents.ip"), primary_key=True)
    generation: Mapped[int]
    restart_generation: Mapped[int]
    force_update_generation: Mapped[int]
    desired_running: Mapped[bool]
    version: Mapped[str] = mapped_column(ForeignKey("version_deployments.version"))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class DesiredStateHistoryRow(Base):
    __tablename__ = "desired_state_history"

    ip: Mapped[str] = mapped_column(ForeignKey("agents.ip"), primary_key=True)
    generation: Mapped[int] = mapped_column(primary_key=True)
    restart_generation: Mapped[int]
    force_update_generation: Mapped[int]
    desired_running: Mapped[bool]
    version: Mapped[str] = mapped_column(ForeignKey("version_deployments.version"))
    changed_at: Mapped[datetime] = mapped_column(UtcDateTime())
