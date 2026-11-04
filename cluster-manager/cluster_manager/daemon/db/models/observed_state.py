from datetime import datetime

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime
from cluster_manager.daemon.wire import AgentStatus, ProcessStatus


class ObservedState(Base):
    __tablename__ = "observed_state"

    ip: Mapped[str] = mapped_column(ForeignKey("agents.ip"), primary_key=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(UtcDateTime())     # the daemon's clock
    acknowledged_generation: Mapped[int]
    handled_restart_generation: Mapped[int]
    handled_force_update_generation: Mapped[int]
    failed_generation: Mapped[int | None]
    running_version: Mapped[str | None]
    process_status: Mapped[ProcessStatus | None]
    healthy: Mapped[bool | None]
    violations: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))     # NULL and [] stay distinct
    metrics_available: Mapped[bool | None]
    agent_status: Mapped[AgentStatus]
    current_operation: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    last_operation: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
