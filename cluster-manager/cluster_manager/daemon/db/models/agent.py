from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from cluster_manager.daemon.db.base import Base, UtcDateTime


class Agent(Base):
    __tablename__ = "agents"

    ip: Mapped[str] = mapped_column(primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime())
    decommissioned_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
