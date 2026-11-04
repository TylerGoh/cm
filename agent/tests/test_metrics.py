import asyncio

import pytest

from agent import metrics
from agent.feed import ConditionFeed, Conditions
from agent.metrics import MetricsReading, PerformanceMonitor, judge
from agent.wire import ProcessStatus
from agent.worker_client import WorkerMetrics

WINDOW = 300.0
INTERVAL = 30.0


def reading(at: float, out: float, old_failed: float = 0, lagged: float = 0) -> MetricsReading:
    return MetricsReading(at=at, values=WorkerMetrics.model_validate({
        "requests.out.total": out,
        "requests.old_failed.total": old_failed,
        "requests.out_lagged.total": lagged,
    }))


class FakeWorker:
    """Returns the queued readings' values in order; None means a failed call."""
    def __init__(self) -> None:
        self.queue: list[WorkerMetrics | None] = []

    async def metrics(self) -> WorkerMetrics | None:
        return self.queue.pop(0)


def running(feed: ConditionFeed, busy: bool = False) -> None:
    feed.publish(Conditions(process_status=ProcessStatus.RUNNING, busy=busy,
                            updating=False, running_version="1.0"))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(metrics.time, "monotonic", lambda: now[0])
    return now


def monitor() -> tuple[PerformanceMonitor, FakeWorker, ConditionFeed]:
    worker, feed = FakeWorker(), ConditionFeed()
    running(feed)
    m = PerformanceMonitor(worker, feed, interval=INTERVAL, window_seconds=WINDOW,  # type: ignore[arg-type]
                           max_failed_ratio=0.30, max_failures=3)
    return m, worker, feed


def step(m: PerformanceMonitor, worker: FakeWorker, clock: list[float], r: MetricsReading | None) -> None:
    worker.queue.append(r.values if r is not None else None)
    asyncio.run(m._step())
    clock[0] += INTERVAL


# --- judge ---

def test_judge_counts_both_failures_against_intake() -> None:
    # 60 out, 20 old + 20 lagged failed: 40 of 100 = 40%
    assert judge(reading(0, 0), reading(300, 60, 20, 20), 0.30) != []


def test_judge_at_threshold_is_ok() -> None:
    assert judge(reading(0, 0), reading(300, 70, 15, 15), 0.30) == []        # exactly 30%


def test_judge_uses_the_window_not_the_totals() -> None:
    # Many failures before the window, none during it.
    assert judge(reading(0, 100, 900, 0), reading(300, 200, 900, 0), 0.30) == []


def test_judge_no_traffic_is_ok() -> None:
    assert judge(reading(0, 5, 1, 1), reading(300, 5, 1, 1), 0.30) == []


# --- the monitor ---

def test_no_verdict_until_a_full_window(clock: list[float]) -> None:
    m, worker, _ = monitor()
    for i in range(10):                             # 0s .. 270s
        step(m, worker, clock, reading(clock[0], out=10 * i))
    assert m.violations is None
    step(m, worker, clock, reading(clock[0], out=100))   # 300s: full window
    assert m.violations == []


def test_bad_window_reports_a_violation(clock: list[float]) -> None:
    m, worker, _ = monitor()
    for i in range(11):                             # 0s .. 300s, half the requests fail
        step(m, worker, clock, reading(clock[0], out=10 * i, lagged=10 * i))
    assert m.violations is not None and len(m.violations) == 1


def test_counter_reset_starts_a_new_window(clock: list[float]) -> None:
    m, worker, _ = monitor()
    for i in range(11):
        step(m, worker, clock, reading(clock[0], out=100 + 10 * i))
    assert m.violations == []
    step(m, worker, clock, reading(clock[0], out=5))     # counters went down: restarted
    assert m.violations is None


def test_operation_resets_everything(clock: list[float]) -> None:
    m, worker, feed = monitor()
    for i in range(11):
        step(m, worker, clock, reading(clock[0], out=10 * i))
    running(feed, busy=True)
    asyncio.run(m._step())
    assert m.violations is None and m.metrics_available is None


def test_metrics_unavailable_after_three_failures(clock: list[float]) -> None:
    m, worker, _ = monitor()
    for i in range(11):
        step(m, worker, clock, reading(clock[0], out=10 * i))
    step(m, worker, clock, None)
    step(m, worker, clock, None)
    assert m.metrics_available is True and m.violations == []   # two misses keep the last state
    step(m, worker, clock, None)
    assert m.metrics_available is False and m.violations is None


def test_success_clears_unavailable(clock: list[float]) -> None:
    m, worker, _ = monitor()
    for _ in range(3):
        step(m, worker, clock, None)
    assert m.metrics_available is False
    step(m, worker, clock, reading(clock[0], out=1))
    assert m.metrics_available is True


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError):
        PerformanceMonitor(FakeWorker(), ConditionFeed(), interval=300, window_seconds=300,  # type: ignore[arg-type]
                           max_failed_ratio=0.3, max_failures=3)
    with pytest.raises(ValueError):
        PerformanceMonitor(FakeWorker(), ConditionFeed(), interval=30, window_seconds=300,  # type: ignore[arg-type]
                           max_failed_ratio=1.5, max_failures=3)
