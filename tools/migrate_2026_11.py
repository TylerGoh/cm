#!/usr/bin/env python3
"""Moves the daemon's database to the 2026-11-13 schema by wiping it: every table and enum type
is dropped and the current schema created empty, in one transaction. Nothing is kept: versions,
agents, desired and observed state, operations and incidents all go. Agents register again on
their next heartbeat; the daemon's next start is a first boot, which fetches BOOTSTRAP_VERSION
from BOOTSTRAP_UPSTREAM_URL and makes it ACTIVE.

Before running: set BOOTSTRAP_VERSION and BOOTSTRAP_UPSTREAM_URL to a good version, add IPV4,
and on the file server delete that version's folder under FILE_SERVER_ROOT (or all of them), or
the bootstrap fetch is handed back the stored copy. Then, on the daemon's box, with the daemon's
own Python (it imports the installed cluster_manager, whose config reads its .env):

    scp tools/migrate_2026_11.py <daemon box>:/tmp/
    systemctl stop cluster-manager
    /opt/cluster-manager/.venv/bin/python /tmp/migrate_2026_11.py --yes
    systemctl start cluster-manager

Refuses without --yes, and refuses a database already on this schema (incidents.source exists),
so a second run can't wipe what the daemon has built since.
"""

import argparse
import asyncio
import sys

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.base import Base
from cluster_manager.daemon.db.models import (  # noqa: F401  (every table into Base.metadata)
    agent, desired_state, incident, observed_state, operation, version_deployment,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really drop everything")
    args = parser.parse_args()

    if not Base.metadata.tables:
        sys.exit("migrate: no models loaded, so nothing could be recreated; nothing changed")

    engine = create_async_engine(config.database_url)
    try:
        async with engine.begin() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            if "incidents" in tables and await _has_column(conn, "incidents", "source"):
                sys.exit("migrate: already on the new schema (incidents.source exists); nothing changed")
            types = (await conn.execute(text(
                "SELECT typname FROM pg_type JOIN pg_namespace ON pg_namespace.oid = typnamespace "
                "WHERE nspname = current_schema() AND typtype = 'e'"
            ))).scalars().all()
            print(f"tables: {', '.join(sorted(tables)) or 'none'}")
            print(f"enum types: {', '.join(sorted(types)) or 'none'}")
            if not args.yes:
                sys.exit("migrate: pass --yes to drop all of them; nothing changed")

            for table in tables:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
            for name in types:
                await conn.execute(text(f'DROP TYPE IF EXISTS "{name}" CASCADE'))
            await conn.run_sync(Base.metadata.create_all)
            created = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            if set(created) != set(Base.metadata.tables):
                raise RuntimeError(f"created {sorted(created)}, expected {sorted(Base.metadata.tables)}")
    finally:
        await engine.dispose()
    print("wiped; the new schema is in place, empty. The daemon's next start bootstraps "
          f"{config.bootstrap_version} from {config.bootstrap_upstream_url}")


async def _has_column(conn: AsyncConnection, table: str, column: str) -> bool:
    names = await conn.run_sync(lambda sync_conn: [c["name"] for c in inspect(sync_conn).get_columns(table)])
    return column in names


if __name__ == "__main__":
    asyncio.run(main())
