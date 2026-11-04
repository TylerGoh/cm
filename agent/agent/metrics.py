import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from agent.feed import ConditionFeed
from agent.wire import ProcessStatus
from agent.worker_client import COUNTER_FIELDS, WorkerClient, WorkerMetrics

log = logging.getLogger("agent.metrics")


@dataclass(frozen=True, slots=True)
class MetricsReading:
    at: float                       # monotonic
    values: WorkerMetrics


# --- pure functions ---

def failed_requests(reading: MetricsReading) -> float:
    """Requests that should have gone out but didn't: too old, or lagged."""
    v = reading.values
    return v.requests_old_failed_total + v.requests_out_lagged_total


def intake_requests(reading: MetricsReading) -> float:
    """Everything that came in: what went out plus what failed."""
    return reading.values.requests_out_total + failed_requests(reading)


def counter_reset(prev: MetricsReading, curr: MetricsReading) -> bool:
    """Any counter went down, so the worker restarted and the counters started again."""
    return any(getattr(curr.values, name) < getattr(prev.values, name) for name in COUNTER_FIELDS)


def judge(oldest: MetricsReading, newest: MetricsReading, max_failed_ratio: float) -> list[str]:
    """[] = ok, one message = too many failed. The counters are cumulative, so the amount over
    the window is newest - oldest. No intake in the window means nothing failed: ok."""
    failed = failed_requests(newest) - failed_requests(oldest)
    intake = intake_requests(newest) - intake_requests(oldest)
    if intake == 0:
        return []
    ratio = failed / intake
    if ratio > max_failed_ratio:
        minutes = (newest.at - oldest.at) / 60
        return [f"{ratio:.0%} of requests failed over {minutes:.1f} min "
                f"({failed:.0f} of {intake:.0f}), above {max_failed_ratio:.0%}"]
    return []


# --- the monitor ---

class PerformanceMonitor:
    """Judges the worker from its failed-request ratio over a sliding window. Active while the
    worker is RUNNING and no operation runs; otherwise it forgets everything, so every verdict
    describes one uninterrupted run of one version."""

    def __init__(self, worker: WorkerClient, feed: ConditionFeed, *, interval: float,
                 window_seconds: float, max_failed_ratio: float, max_failures: int) -> None:
        if not 0 < interval < window_seconds:
            raise ValueError(f"need 0 < interval < window_seconds, got {interval} and {window_seconds}")
        if not 0 < max_failed_ratio < 1:
            raise ValueError(f"max_failed_ratio must be between 0 and 1, got {max_failed_ratio}")
        if max_failures < 1:
            raise ValueError(f"max_failures must be at least 1, got {max_failures}")
        self._worker = worker
        self._feed = feed
        self._interval = interval                   # seconds between readings
        self._window_seconds = window_seconds       # judged span, e.g. 300
        self._max_failed_ratio = max_failed_ratio   # e.g. 0.30
        self._max_failures = max_failures           # failed metrics() calls in a row before False
        self._readings: deque[MetricsReading] = deque()
        self._failures = 0
        self._violations: list[str] | None = None
        self._metrics_available: bool | None = None

    @property
    def violations(self) -> list[str] | None:
        """None = no verdict (no full window yet, inactive, or metrics unavailable),
        [] = ok, list = problems."""
        return self._violations

    @property
    def metrics_available(self) -> bool | None:
        """None = no data yet or inactive, True = the last metrics() call succeeded,
        False = max_failures calls failed in a row."""
        return self._metrics_available

    async def run(self) -> None:
        while True:
            try:
                await self._step()
            except Exception:
                log.exception("performance step failed")
            await asyncio.sleep(self._interval)

    async def _step(self) -> None:
        if not self._active():
            self._reset()
            return
        values = await self._worker.metrics()
        if not self._active():                      # an operation may have started during the read
            self._reset()
            return
        if values is None:
            self._failures += 1
            if self._failures >= self._max_failures:
                self._metrics_available = False
                self._violations = None             # no data, so no verdict
            return
        self._failures = 0
        self._metrics_available = True
        self._add(MetricsReading(at=time.monotonic(), values=values))
        self._violations = self._judge()

    def _active(self) -> bool:
        cond = self._feed.current()
        return cond.process_status is ProcessStatus.RUNNING and not cond.busy

    def _add(self, curr: MetricsReading) -> None:
        """Appends the reading and drops readings the window no longer needs. Keeps the newest
        reading at or before the window's start, so the judged span covers the whole window."""
        if self._readings and counter_reset(self._readings[-1], curr):
            self._readings.clear()                  # the worker restarted: start a new window
        self._readings.append(curr)
        window_start = curr.at - self._window_seconds
        while len(self._readings) >= 2 and self._readings[1].at <= window_start:
            self._readings.popleft()

    def _judge(self) -> list[str] | None:
        oldest, newest = self._readings[0], self._readings[-1]
        if newest.at - oldest.at < self._window_seconds:
            return None                             # not a full window yet
        return judge(oldest, newest, self._max_failed_ratio)

    def _reset(self) -> None:
        self._readings.clear()
        self._failures = 0
        self._violations = None
        self._metrics_available = None
