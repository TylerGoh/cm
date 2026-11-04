"""The engine, the session factory and schema creation. SQLite through aiosqlite.

Every connection turns on foreign keys (SQLite leaves them off otherwise, so every ForeignKey in
the models would go unenforced) and WAL mode (readers don't block the one writer)."""

import sqlite3
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.base import Base
from cluster_manager.daemon.db.models import (  # noqa: F401  (imported so create_all sees every table)
    agent, desired_state, incident, observed_state, operation, version_deployment,
)

MIN_SQLITE = (3, 35, 0)             # RETURNING

engine = create_async_engine(config.database_url)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _on_connect(dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old; RETURNING needs 3.35 or later")


async def create_schema() -> None:
    """Creates missing tables. While the data can still be wiped there are no migrations: a
    changed model means dropping the database file."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. The services commit; anything left
    uncommitted is rolled back when the session closes."""
    async with session_factory() as session:
        yield session
