"""Opens a non_responding incident for every live agent silent past the threshold. Runs every
tick; opening is idempotent (one open incident per agent and kind), so an agent that stays silent
is announced once. The agent's next heartbeat closes the incident."""

import asyncio
import logging
from datetime import UTC, datetime

from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.repositories.incident_repo import IncidentRepo
from cluster_manager.daemon.db.repositories.observed_state_repo import ObservedStateRepo
from cluster_manager.daemon.db.session import session_factory
from cluster_manager.schemas import IncidentKind, IncidentSource

log = logging.getLogger("cluster_manager.watcher")

WATCH_INTERVAL = 10.0               # one agent tick


async def run() -> None:
    while True:
        try:
            await _step()
        except Exception:
            log.exception("watcher step failed")
        await asyncio.sleep(WATCH_INTERVAL)


async def _step() -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        silent = await ObservedStateRepo(session).silent_ips(now, config.non_responding_after)
        incidents = IncidentRepo(session)
        for ip in silent:
            opened = await incidents.open(ip, IncidentKind.NON_RESPONDING, now, source=IncidentSource.AGENT)
            if opened is not None:
                log.warning("%s is non-responding", ip)
        await session.commit()
