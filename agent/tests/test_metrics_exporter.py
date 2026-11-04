import asyncio

from agent.feed import ConditionFeed, Conditions
from agent.metrics_exporter import DOCUMENT_KEYS, MetricsExporter
from agent.wire import ProcessStatus
from agent.worker_client import METRIC_SOURCES, WorkerMetrics


def reading() -> WorkerMetrics:
    """One reading, built from the worker's names (the intake side), like WorkerClient does."""
    names = [name for names in METRIC_SOURCES.values() for name in sorted(names)]
    return WorkerMetrics.model_validate({name: float(i) for i, name in enumerate(names)})


class FakeWorker:
    def __init__(self, answer: WorkerMetrics | None) -> None:
        self.answer = answer
        self.calls = 0

    async def metrics(self) -> WorkerMetrics | None:
        self.calls += 1
        return self.answer


class FakeElastic:
    def __init__(self) -> None:
        self.documents: list[dict[str, object]] = []

    async def index_metrics(self, document: dict[str, object]) -> None:
        self.documents.append(document)


def exporter(status: ProcessStatus | None, answer: WorkerMetrics | None) -> tuple[MetricsExporter, FakeWorker, FakeElastic]:
    feed = ConditionFeed()
    feed.publish(Conditions(process_status=status, busy=False, updating=False, running_version="job_7"))
    worker, elastic = FakeWorker(answer), FakeElastic()
    return MetricsExporter(worker, elastic, feed, interval=30), worker, elastic  # type: ignore[arg-type]


def test_a_running_worker_is_exported_under_the_field_names() -> None:
    values = reading()
    m, _, elastic = exporter(ProcessStatus.RUNNING, values)
    asyncio.run(m._step())
    [document] = elastic.documents
    assert document["running_version"] == "job_7" and document["updating"] is False and "at" in document
    for field in WorkerMetrics.model_fields:                       # the Elastic name is the field name
        assert document[field] == getattr(values, field)
    worker_names = {name for names in METRIC_SOURCES.values() for name in names}
    assert not worker_names & document.keys()                      # the worker's names never leave the agent


def test_nothing_is_read_or_sent_unless_running() -> None:
    for status in (None, ProcessStatus.STOPPED, ProcessStatus.CRASHED, ProcessStatus.STARTING):
        m, worker, elastic = exporter(status, reading())
        asyncio.run(m._step())
        assert worker.calls == 0 and elastic.documents == []


def test_a_failed_reading_sends_nothing() -> None:
    m, _, elastic = exporter(ProcessStatus.RUNNING, None)
    asyncio.run(m._step())
    assert elastic.documents == []


def test_no_metric_field_clashes_with_the_document_keys() -> None:
    assert not DOCUMENT_KEYS & WorkerMetrics.model_fields.keys()
