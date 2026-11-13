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
`WorkerMetrics`) and calls `judge(oldest, newest)` once the window is full. `judge` is yours: it
lives in the user block, `# BEGIN user: judge` / `# END user: judge`, which the repo ships
commented out. Until you fill it in, the first full window fails with `NameError` and the agent
logs "performance step failed" every interval, with no verdict. The rest of the monitor (the
window, the gate, `metrics_available`) doesn't depend on the fields.

- A **counter** is cumulative, so what happened in the window is `newest - oldest`.
- A **gauge** is a current value, so read it from `newest`; it means nothing to subtract.

A failed-request ratio, as a pattern for yours (helpers and limits go inside the block too):

```python
# BEGIN user: judge
MAX_FAILED_RATIO = 0.30


def failed_requests(reading: MetricsReading) -> float:
    v = reading.values
    return v.requests_old_failed_total + v.requests_out_lagged_total


def intake_requests(reading: MetricsReading) -> float:
    return reading.values.requests_out_total + failed_requests(reading)


def judge(oldest: MetricsReading, newest: MetricsReading) -> list[str] | None:
    failed = failed_requests(newest) - failed_requests(oldest)
    intake = intake_requests(newest) - intake_requests(oldest)
    if intake == 0:
        return []
    ratio = failed / intake
    if ratio > MAX_FAILED_RATIO:
        minutes = (newest.at - oldest.at) / 60
        return [f"{ratio:.0%} of requests failed over {minutes:.1f} min "
                f"({failed:.0f} of {intake:.0f}), above {MAX_FAILED_RATIO:.0%}"]
    return []
# END user: judge
```

Keep to these rules, since the cluster manager depends on them:
- Return `[]` for fine, messages only for problems, and `None` for no verdict. The messages are
  for humans and nothing parses them, so say what was measured and the limit.
- A counter that went down means the worker restarted; the monitor already starts a new window
  when any counter drops, so `judge` never sees a reset.
- Downstream, the reading is flat and dot-free, so it exports cleanly.
