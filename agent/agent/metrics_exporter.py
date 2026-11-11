import asyncio
import logging
import time
from datetime import UTC, datetime

from agent.elastic import ElasticClient
from agent.feed import ConditionFeed
from agent.wire import ProcessStatus
from agent.worker_client import WorkerClient, WorkerMetrics

log = logging.getLogger("agent.metrics_exporter")

# Keys every metrics document carries besides the metric fields: these four from here, and
# hostname and ipv4 from ElasticClient. No WorkerMetrics field may use one of them; prepare.py
# refuses them, and so does the constructor.
DOCUMENT_KEYS = frozenset({"@timestamp", "at", "running_version", "updating", "hostname", "ipv4"})


class MetricsExporter:
    """Every interval, while the worker is RUNNING, reads the worker's metrics and sends one
    document to Elastic's metrics index: each WorkerMetrics field under its field name, plus
    `@timestamp` and `at` (the same UTC time), `running_version` and `updating`. A metric has
    two names: the worker's (its validation_alias, used only to read the worker's JSON) and the
    field name, which is its name in Elastic. Counters are sent as the worker reports them, cumulative; a dashboard derives
    rates. It judges nothing: the performance monitor takes its own readings."""

    def __init__(self, worker: WorkerClient, elastic: ElasticClient, feed: ConditionFeed, *,
                 interval: float) -> None:
        clashing = DOCUMENT_KEYS & WorkerMetrics.model_fields.keys()
        if clashing:
            raise ValueError(f"WorkerMetrics fields clash with metrics document keys: {sorted(clashing)}")
        self._worker = worker
        self._elastic = elastic
        self._feed = feed
        self._interval = interval

    async def run(self) -> None:
        next_at = time.monotonic()
        while True:
            try:
                await self._step()
            except Exception:
                log.exception("metrics export step failed")
            next_at = max(next_at + self._interval, time.monotonic())     # steady cadence
            await asyncio.sleep(next_at - time.monotonic())

    async def _step(self) -> None:
        cond = self._feed.current()
        if cond.process_status is not ProcessStatus.RUNNING:
            return                                  # nothing to read; the inventory records the status
        reading = await self._worker.metrics()
        if reading is None:
            return                                  # the performance monitor reports metrics_available
        now = datetime.now(UTC).isoformat()
        document: dict[str, object] = {
            "@timestamp": now,                      # Elastic's time field, for data views and dashboards
            "at": now,
            "running_version": cond.running_version,
            "updating": cond.updating,
        }
        document.update(reading.model_dump())       # field names: the names in Elastic
        await self._elastic.index_metrics(document)
