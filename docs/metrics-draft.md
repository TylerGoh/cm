# Parked: first draft of performance judging

Not live code. `agent/metrics.py` is a blank `PerformanceMonitor` for now (see `decisions.md`, 2026-10-27). This is the first draft that was in `agent/metrics.py` before that, kept so the design isn't lost. The `MetricsSummary` fields, the checks and their thresholds are mock. When judging comes back, take pieces from here into `agent/metrics.py` and delete this file.

Review notes from 2026-10-27, to apply if this comes back:
- Re-check the gate after `metrics()` returns: an operation can start while the read is in flight, and an in-place reload restarts nothing, so `counter_reset` and `restarted` won't catch it. Check `worker is RUNNING and not busy` again rather than comparing whole `Conditions`, so a one-tick `running_version` of `None` doesn't reset the monitor.
- Validate settings in `__init__`: `0 < min_bins <= window_bins`, `0 <= max_bad_bins < window_bins`, `stale_after > bin_seconds`. Otherwise a bad value silently means "never a verdict" or "never bad".
- `_document` has no `host` field; how documents reach Elastic is undecided (see `open-items.md`).

```python
import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from agent.elastic import ElasticClient
from agent.feed import ConditionFeed
from agent.wire import ProcessStatus
from agent.worker_client import COUNTER_FIELDS, GAUGE_FIELDS, WorkerClient, WorkerMetrics

log = logging.getLogger("agent.metrics")


@dataclass(frozen=True, slots=True)
class MetricsReading:
    at: float; wall: float; values: WorkerMetrics        # at = monotonic


@dataclass(frozen=True, slots=True)
class MetricsSummary:
    """One bin, flat. Derived fields come from counter deltas; gauge fields pass through
    from the current reading and must match WorkerMetrics names exactly. First draft:
    the real fields are decided later."""
    elapsed: float                  # seconds between the two readings

    # derived from counter deltas
    requests_per_s: float
    bytes_out_per_s: float
    error_rate: float               # errors / requests in the bin, 0.0 if no requests
    timeout_rate: float

    # passthrough gauges
    queue_depth: float
    active_connections: float
    latency_p99_ms: float
    uptime_seconds: float


@dataclass(frozen=True, slots=True)
class MetricsBin:
    started_at: float               # wall clock of the earlier reading
    summary: MetricsSummary
    problems: tuple[str, ...]       # empty = good bin


@dataclass(frozen=True, slots=True)
class PerformanceVerdict:
    at: float                       # monotonic
    version: str | None
    violations: list[str] | None    # None = no verdict, [] = ok, list = problems


type PerformanceCheck = Callable[[MetricsSummary], str | None]   # None = fine, otherwise a reason


# --- checks (mock thresholds) ---

MAX_ERROR_RATE = 0.05
MAX_TIMEOUT_RATE = 0.02
MAX_P99_MS = 500.0
MAX_QUEUE_DEPTH = 100.0


def error_rate_high(s: MetricsSummary) -> str | None:
    if s.error_rate > MAX_ERROR_RATE:
        return f"error rate {s.error_rate:.1%} > {MAX_ERROR_RATE:.0%}"
    return None


def timeout_rate_high(s: MetricsSummary) -> str | None:
    if s.timeout_rate > MAX_TIMEOUT_RATE:
        return f"timeout rate {s.timeout_rate:.1%} > {MAX_TIMEOUT_RATE:.0%}"
    return None


def latency_high(s: MetricsSummary) -> str | None:
    if s.latency_p99_ms > MAX_P99_MS:
        return f"p99 latency {s.latency_p99_ms:.0f}ms > {MAX_P99_MS:.0f}ms"
    return None


def overloaded(s: MetricsSummary) -> str | None:
    # combined score over three metrics: deep queue while slow and busy
    if s.queue_depth > MAX_QUEUE_DEPTH and s.latency_p99_ms > MAX_P99_MS / 2 and s.active_connections > 0:
        return (f"overloaded: queue {s.queue_depth:.0f}, p99 {s.latency_p99_ms:.0f}ms, "
                f"{s.active_connections:.0f} connections")
    return None


PERFORMANCE_CHECKS: list[PerformanceCheck] = [error_rate_high, timeout_rate_high, latency_high, overloaded]


# --- pure functions ---

def _ratio(part: float, whole: float) -> float:
    return part / whole if whole > 0 else 0.0


def counter_deltas(prev: MetricsReading, curr: MetricsReading) -> dict[str, float]:
    """Amount of each counter in this bin: curr - prev."""
    return {name: getattr(curr.values, name) - getattr(prev.values, name) for name in COUNTER_FIELDS}


def gauge_values(curr: MetricsReading) -> dict[str, float]:
    """Snapshot of each gauge: taken from curr as-is."""
    return {name: getattr(curr.values, name) for name in GAUGE_FIELDS}


def summarize(prev: MetricsReading, curr: MetricsReading) -> MetricsSummary:
    elapsed = curr.at - prev.at
    d = counter_deltas(prev, curr)
    return MetricsSummary(
        elapsed=elapsed,
        requests_per_s=d["requests_total"] / elapsed,
        bytes_out_per_s=d["bytes_out_total"] / elapsed,
        error_rate=_ratio(d["errors_total"], d["requests_total"]),
        timeout_rate=_ratio(d["timeouts_total"], d["requests_total"]),
        **gauge_values(curr),
    )


def evaluate(summary: MetricsSummary, checks: Sequence[PerformanceCheck]) -> list[str]:
    return [reason for check in checks if (reason := check(summary)) is not None]


def close_bin(prev: MetricsReading, curr: MetricsReading) -> MetricsBin:     # pure; runs in to_thread
    summary = summarize(prev, curr)
    return MetricsBin(started_at=prev.wall, summary=summary,
                      problems=tuple(evaluate(summary, PERFORMANCE_CHECKS)))


def counter_reset(prev: MetricsReading, curr: MetricsReading) -> bool:       # any counter went down
    return any(getattr(curr.values, name) < getattr(prev.values, name) for name in COUNTER_FIELDS)


def restarted(prev: MetricsReading, curr: MetricsReading) -> bool:           # uptime went down
    return curr.values.uptime_seconds < prev.values.uptime_seconds


def judge(bins: Sequence[MetricsBin], max_bad: int, min_bins: int) -> list[str] | None:
    """None if fewer than min_bins; [] if bad bins <= max_bad; else one summary line."""
    if len(bins) < min_bins:
        return None
    bad = [b for b in bins if b.problems]
    if len(bad) <= max_bad:
        return []
    latest = "; ".join(bad[-1].problems)
    return [f"{len(bad)} of {len(bins)} bins bad (max {max_bad}); latest: {latest}"]


# --- the monitor ---

class PerformanceMonitor:
    def __init__(self, worker: WorkerClient, elastic: ElasticClient, feed: ConditionFeed, *,
                 bin_seconds: float, window_bins: int, max_bad_bins: int, min_bins: int,
                 stale_after: float) -> None:
        self._worker = worker
        self._elastic = elastic
        self._feed = feed
        self._bin_seconds = bin_seconds
        self._max_bad_bins = max_bad_bins
        self._min_bins = min_bins
        self._stale_after = stale_after             # should be more than bin_seconds
        self._prev: MetricsReading | None = None
        self._bins: deque[MetricsBin] = deque(maxlen=window_bins)
        self._verdict: PerformanceVerdict | None = None

    @property
    def violations(self) -> list[str] | None:
        """None = no verdict (or stale), [] = ok, list = problems."""
        v = self._verdict
        if v is None or time.monotonic() - v.at > self._stale_after:
            return None
        return v.violations

    async def run(self) -> None:
        next_at = time.monotonic()
        while True:
            try:
                await self._step()
            except Exception:
                log.exception("performance step failed")
            next_at = max(next_at + self._bin_seconds, time.monotonic())     # steady cadence
            await asyncio.sleep(next_at - time.monotonic())

    async def _step(self) -> None:
        cond = self._feed.current()
        if cond.worker is not ProcessStatus.RUNNING or cond.busy:
            self._reset()
            return

        metrics = await self._worker.metrics()
        if metrics is None:
            return                      # missed reading; a long gap becomes a baseline below

        curr = MetricsReading(at=time.monotonic(), wall=time.time(), values=metrics)
        prev, self._prev = self._prev, curr
        if (prev is None
                or curr.at - prev.at > 1.5 * self._bin_seconds
                or counter_reset(prev, curr)
                or restarted(prev, curr)):
            return                      # baseline only, no bin

        closed = await asyncio.to_thread(close_bin, prev, curr)
        self._bins.append(closed)
        self._verdict = PerformanceVerdict(
            at=curr.at,
            version=cond.running_version,
            violations=judge(self._bins, self._max_bad_bins, self._min_bins),
        )
        await self._elastic.index(self._document(closed, self._verdict))

    def _reset(self) -> None:
        self._prev = None
        self._bins.clear()
        self._verdict = None

    @staticmethod
    def _document(closed: MetricsBin, verdict: PerformanceVerdict) -> dict[str, object]:
        return {
            "started_at": datetime.fromtimestamp(closed.started_at, UTC).isoformat(),
            "version": verdict.version,
            **asdict(closed.summary),
            "problems": list(closed.problems),
            "violations": verdict.violations,
        }
```
