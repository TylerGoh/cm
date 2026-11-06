# The worker contract and the performance judgement

The worker contract (the worker's commands, its metric commands and the `WorkerMetrics` model) is
the generated block in `agent/agent/worker_client.py`, written by `prepare.py` from
`config.yaml`'s `worker:` section (see `config.example.yaml`). Nothing else in
`worker_client.py` changes: `_derive()` works out which endpoint each name is fetched from, and
which fields are counters or gauges, from the block itself.

Changing a metric's `field` means changing `agent/agent/performance.py`, which reads fields by
name, and the agent's tests (`tests/test_performance.py` builds readings with the field names).

## The performance judgement (sample)

`performance.py` keeps a window of readings (`MetricsReading(at, values)`, where `values` is a
`WorkerMetrics`) and calls `judge(oldest, newest, max_failed_ratio)` once the window is full.
`judge` returns `[]` when the worker is fine, or a list of messages for humans when it isn't.
The rest of the monitor (the window, the gate, `metrics_available`) doesn't depend on the
fields, so `judge` and the helpers it uses are all you rewrite.

- A **counter** is cumulative, so what happened in the window is `newest - oldest`.
- A **gauge** is a current value, so read it from `newest`; it means nothing to subtract.

Today's judgement, as a pattern for yours:

```python
def failed_requests(reading: MetricsReading) -> float:
    """Requests that should have gone out but didn't: too old, or lagged."""
    v = reading.values
    return v.requests_old_failed_total + v.requests_out_lagged_total


def intake_requests(reading: MetricsReading) -> float:
    """Everything that came in: what went out plus what failed."""
    return reading.values.requests_out_total + failed_requests(reading)


def judge(oldest: MetricsReading, newest: MetricsReading, max_failed_ratio: float) -> list[str]:
    """[] = ok, one message = too many failed. Counters: the window's amount is newest - oldest.
    No intake in the window means nothing failed: ok."""
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
```

A second check on a gauge is one more `if` that appends its own message:

```python
MAX_QUEUE_DEPTH = 1000

def judge(oldest: MetricsReading, newest: MetricsReading, max_failed_ratio: float) -> list[str]:
    problems: list[str] = []
    ...                                                    # the ratio check above, appending
    if newest.values.queue_depth > MAX_QUEUE_DEPTH:        # a gauge: read the newest value
        problems.append(f"queue depth {newest.values.queue_depth:.0f} > {MAX_QUEUE_DEPTH}")
    return problems
```

Keep to these rules, since the cluster manager depends on them:
- Return `[]` for fine, and messages only for problems. The messages are for humans and nothing
  parses them, so say what was measured and the limit.
- A counter that went down means the worker restarted; the monitor already starts a new window
  when any counter drops, so `judge` never sees a reset.
- A limit you'll want to tune per deployment belongs in the agent's `.env` (`config.py`), passed
  to `PerformanceMonitor` like `max_failed_ratio`, rather than a constant.
