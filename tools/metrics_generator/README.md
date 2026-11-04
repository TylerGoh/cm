# metrics_generator

Writes the worker contract block for `agent/agent/worker_client.py` from a YAML file: the worker's
version and reload paths, its metric endpoints, and the `WorkerMetrics` model. You describe the
worker once in YAML; the script writes the Python to paste.

It does not write the performance judgement. That is yours to code in `agent/agent/performance.py`;
the sample at the end shows the pattern.

## Run it

```bash
pip install pyyaml pydantic            # any virtualenv; the agent's already has pydantic
cp worker.example.yaml worker.yaml     # then put in the real paths and metrics
python generate.py worker.yaml
```

It writes `generated_worker_contract.py` in this folder. Both it and `worker.yaml` are ignored
by git.

## The YAML

```yaml
version_path: /show_version          # answers {"version": "..."}; also the health ping
reload_phase1_path: /reload_phase1   # GET, no body, blocks until done
reload_phase2_path: /reload_phase2   # GET, no body, blocks until the worker runs the new code

endpoints:                           # each a GET answering one flat JSON object
  - path: /stats                     # Python name derived: STATS_ENDPOINT
    metrics:
      - name: req.out                # its key in the JSON, exactly as the worker sends it
        field: requests_out          # the WorkerMetrics field, snake_case
        type: counter                # counter: cumulative since the worker started
      - name: queue.depth
        field: queue_depth
        type: gauge                  # gauge: a current value
        description: requests waiting   # optional; becomes a comment
  - path: /memory
    constant: MEMORY_ENDPOINT        # optional: choose the Python name yourself
    metrics:
      - {name: rss_bytes, field: rss_bytes, type: gauge}
```

One endpoint can mix counters and gauges. Every `field` and every `name` must be unique across
the file. Mistakes are reported with the entry they are in, before anything is written.

## Paste it

`worker_client.py` has the block between two markers:

```python
# --- BEGIN worker contract: ... ---
...
# --- END worker contract ---
```

Replace everything from the BEGIN line to the END line, both included, with the whole generated
file. Nothing else in `worker_client.py` changes: `_derive()` works out which endpoint each name
is fetched from, and which fields are counters or gauges, from the block itself.

Then fix `performance.py`, which reads fields by name, and the agent's tests
(`tests/test_performance.py` and `tests/test_worker_client.py` build readings with the old names).

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
