import asyncio
import logging

from agent.feed import ConditionFeed
from agent.wire import ProcessStatus
from agent.worker_client import WorkerClient

log = logging.getLogger("agent.health")


class HealthMonitor:
    """Liveness of the worker, checked through its version endpoint (the cheapest ping).
    Its False verdict, passed to ProcessManager.derive_status, makes a live, hung worker count
    as CRASHED. So it stays active while CRASHED (no flapping back to RUNNING) and goes inactive
    while any operation runs (expected dips don't count; the worker only stops answering
    during operations the agent drives)."""

    def __init__(self, worker: WorkerClient, feed: ConditionFeed, *,
                 interval: float, max_failures: int) -> None:
        self._worker = worker
        self._feed = feed
        self._interval = interval
        self._max_failures = max_failures           # consecutive failures before reporting unhealthy
        self._failures = 0
        self._healthy: bool | None = None

    @property
    def healthy(self) -> bool | None:
        """None = no data yet, True = ok, False = max_failures failed checks in a row."""
        return self._healthy

    async def run(self) -> None:
        while True:
            try:
                await self._step()
            except Exception:
                log.exception("health step failed")
            await asyncio.sleep(self._interval)

    async def _step(self) -> None:
        cond = self._feed.current()
        if cond.busy or cond.process_status not in (ProcessStatus.RUNNING, ProcessStatus.CRASHED):
            self._healthy = None                    # inactive: nothing old survives an operation
            self._failures = 0
            return
        if await self._worker.health() is True:
            self._failures = 0
            self._healthy = True
            return
        self._failures += 1                         # answered with an error, or didn't answer
        if self._failures >= self._max_failures:
            self._healthy = False
