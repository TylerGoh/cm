"""The daemon: wiring only. Startup creates the schema, seeds or checks the ACTIVE
version, reschedules a pending canary judgement, then starts the watcher and the sender."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from cluster_manager.daemon.api import agents, fleet, heartbeat, versions as versions_api
from cluster_manager.daemon.clients.file_server import FileServerClient
from cluster_manager.daemon.clients.mattermost import MattermostClient
from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.session import create_schema, engine, session_factory
from cluster_manager.daemon.loops import fleet_watch, sender, watcher
from cluster_manager.daemon.services import versions

log = logging.getLogger("cluster_manager")


def _log_if_failed(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("%s stopped", task.get_name(), exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # --- startup ---
    await create_schema()
    file_server = FileServerClient(config.file_server_url, timeout=config.file_server_timeout,
                                   fetch_timeout=config.file_server_fetch_timeout)
    mattermost = MattermostClient(config.mattermost_webhook_url, timeout=config.mattermost_timeout)
    scheduler = AsyncIOScheduler(timezone=UTC)

    async with session_factory() as session:     # before anything reads the ACTIVE version
        active = await versions.ensure_bootstrap(session, file_server, config.bootstrap_version,
                                                 config.bootstrap_upstream_url, datetime.now(UTC))
        log.info("ACTIVE version is %s", active.version)
        await versions.reschedule_judgement(session, scheduler)
    scheduler.start()

    app.state.file_server = file_server
    app.state.scheduler = scheduler
    loops = [
        asyncio.create_task(watcher.run(), name="watcher"),
        asyncio.create_task(sender.run(mattermost), name="sender"),
        asyncio.create_task(fleet_watch.run(), name="fleet_watch"),
    ]
    for task in loops:
        task.add_done_callback(_log_if_failed)

    try:
        yield
    finally:
        # --- shutdown ---
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        scheduler.shutdown(wait=False)
        await file_server.aclose()
        await mattermost.aclose()
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(heartbeat.router)
app.include_router(fleet.router)
app.include_router(agents.router)
app.include_router(versions_api.router)
