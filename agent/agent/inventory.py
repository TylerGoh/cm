import asyncio
import logging
import time
from datetime import UTC, datetime

from agent.elastic import ElasticClient
from agent.feed import ConditionFeed
from agent.wire import ProcessStatus

log = logging.getLogger("agent.inventory")


class InventoryExporter:
    """Every interval, sends Elastic's host index one record of this host's process status,
    whether it's updating, and its running version, all read from the feed. The host's identity
    is added by ElasticClient. With require_running, it sends nothing unless the process is
    RUNNING."""

    def __init__(self, elastic: ElasticClient, feed: ConditionFeed, *,
                 interval: float, require_running: bool) -> None:
        self._elastic = elastic
        self._feed = feed
        self._interval = interval                   # at least one tick; shorter only sends duplicates
        self._require_running = require_running

    async def run(self) -> None:
        next_at = time.monotonic()
        while True:
            try:
                await self._step()
            except Exception:
                log.exception("inventory step failed")
            next_at = max(next_at + self._interval, time.monotonic())     # steady cadence
            await asyncio.sleep(next_at - time.monotonic())

    async def _step(self) -> None:
        cond = self._feed.current()
        if self._require_running and cond.process_status is not ProcessStatus.RUNNING:
            return
        await self._elastic.index_host({
            "at": datetime.now(UTC).isoformat(),
            "process_status": cond.process_status,  # None while not attached
            "updating": cond.updating,
            "running_version": cond.running_version,  # None unless RUNNING
        })
