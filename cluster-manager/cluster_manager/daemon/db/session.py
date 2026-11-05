"""The engine, the session factory and schema creation. PostgreSQL through asyncpg.

Transactions are READ COMMITTED (the default): a write to a row another transaction has changed
waits for it to commit, and the unique indexes hold the invariants (one ACTIVE, one VERIFYING,
one open incident per agent and kind). SERIALIZABLE was tried and fails concurrent heartbeats
from different agents. pool_pre_ping replaces a connection the server dropped (a database
restart) before it is used."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.base import Base
from cluster_manager.daemon.db.models import (  # noqa: F401  (imported so create_all sees every table)
    agent, desired_state, incident, observed_state, operation, version_deployment,
)

engine = create_async_engine(config.database_url, pool_pre_ping=True)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def create_schema() -> None:
    """Creates missing tables. While the data can still be wiped there are no migrations: a
    changed model means wiping the database (see the README)."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. The services commit; anything left
    uncommitted is rolled back when the session closes."""
    async with session_factory() as session:
        yield session
