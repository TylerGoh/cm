from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime
from cluster_manager.daemon.wire import OpType


class Operation(Base):
    __tablename__ = "operations"

    agent_ip: Mapped[str] = mapped_column(ForeignKey("agents.ip"), primary_key=True)
    op_id: Mapped[str] = mapped_column(primary_key=True)
    generation: Mapped[int]
    type: Mapped[OpType]
    started_at: Mapped[datetime] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime] = mapped_column(UtcDateTime())
    failure: Mapped[str | None]
    steps: Mapped[list[dict[str, object]]] = mapped_column(JSON)


# Indexes are SQLAlchemy expressions over the model's attributes (see version_deployment.py).

Index("operations_agent_generation", Operation.agent_ip, Operation.generation)
