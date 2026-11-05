import asyncio
import importlib.metadata
import logging
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from config import config
from agent.cluster_manager_client import ClusterManagerClient
from agent.elastic import ElasticClient
from agent.feed import ConditionFeed
from agent.health import HealthMonitor
from agent.inventory import InventoryExporter
from agent.metrics_exporter import MetricsExporter
from agent.performance import PerformanceMonitor
from agent.models import AgentMemory, HostInfo, ProcessMemory
from agent.process import ProcessManager
from agent.state_store import JsonStore
from agent.supervisor import Supervisor
from agent.updates import UpdateManager
from agent.wire import ProcessStatus
from agent.worker_client import WorkerClient

log = logging.getLogger("agent")

TICK_INTERVAL = 10.0
DISTRIBUTION = "agent"                  # the name in pyproject.toml, for agent_version


async def tick_loop(supervisor: Supervisor) -> None:
    """Ticks every TICK_INTERVAL (fixed, no jitter) until cancelled.
    An exception ends it, and _exit_if_failed stops the process."""
    while True:
        await supervisor.tick()
        await asyncio.sleep(TICK_INTERVAL)


def _exit_if_failed(task: asyncio.Task[None]) -> None:
    """The tick loop died: stop the whole process so systemd restarts the agent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.critical("%s stopped", task.get_name(), exc_info=exc)
        os.kill(os.getpid(), signal.SIGTERM)        # uvicorn runs the normal shutdown


def _log_if_failed(task: asyncio.Task[None]) -> None:
    """A monitor or the exporter died: log it, keep the supervisor running."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("%s stopped", task.get_name(), exc_info=exc)


def _ensure_first_boot(memory_store: JsonStore[AgentMemory],
                       process_store: JsonStore[ProcessMemory]) -> None:
    """Missing file = first boot, built explicitly. A corrupt file raises and fails startup."""
    try:
        memory_store.load()
    except FileNotFoundError:
        memory_store.save(AgentMemory(acknowledged_generation=0, handled_restart_generation=0,
                                      handled_force_update_generation=0, failed_generation=None,
                                      crash_restarted=False))
    try:
        process_store.load()
    except FileNotFoundError:
        process_store.save(ProcessMemory(pid=None, create_time=None, state=ProcessStatus.STOPPED))


def _agent_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"                            # running from a source tree that isn't installed


async def _record_start(elastic: ElasticClient) -> None:
    """One document per agent start, to the host index. Best effort, like every Elastic write."""
    await elastic.index_host({
        "event": "agent_started",
        "at": datetime.now(UTC).isoformat(),
        "agent_version": _agent_version(),
    })


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # --- startup ---
    memory_store = JsonStore(AgentMemory, config.memory_store_path)
    process_store = JsonStore(ProcessMemory, config.process_store_path)
    _ensure_first_boot(memory_store, process_store)

    host = HostInfo(ipv4=config.ipv4, hostname=config.hostname, location=config.location)
    feed = ConditionFeed()
    worker = WorkerClient(config.worker_url, timeout=config.worker_timeout,
                          reload_timeout=config.reload_timeout)
    elastic = ElasticClient(config.elastic_url, host, cert_path=config.elastic_cert_path,
                            api_key=config.elastic_api_key.get_secret_value(),
                            metrics_index=config.elastic_metrics_index,
                            host_index=config.elastic_host_index, timeout=config.elastic_timeout)
    cluster_manager = ClusterManagerClient(config.cluster_manager_url)
    process = ProcessManager(worker, process_store, command=config.worker_command,
                             console_log_dir=config.worker_console_log_dir,
                             program_log_path=config.worker_program_log_path,
                             initialize_flag=config.worker_initialize_flag,
                             start_timeout=config.start_timeout, stop_timeout=config.stop_timeout)
    updates = UpdateManager(worker, download_dir=config.download_dir, bundle_dir=config.bundle_dir,
                            etc_dir=config.etc_dir,
                            bundle_symlink_mapping=config.bundle_symlink_mapping,
                            bundle_symlink_default_dir=(Path(config.bundle_symlink_default_dir)
                                                        if config.bundle_symlink_default_dir else None),
                            hypertree_dir=config.hypertree_dir,
                            download_timeout=config.download_timeout,
                            hypertree_timeout=config.hypertree_timeout,
                            verify_timeout=config.verify_timeout,
                            verify_interval=config.verify_interval)
    health = HealthMonitor(worker, feed, interval=config.health_interval,
                           max_failures=config.max_failures)
    performance = PerformanceMonitor(worker, feed, interval=config.performance_interval,
                                     window_seconds=config.performance_window_seconds,
                                     max_failed_ratio=config.max_failed_ratio,
                                     max_failures=config.max_metrics_failures)
    inventory = InventoryExporter(elastic, feed, interval=config.inventory_interval,
                                  require_running=config.require_running)
    metrics_exporter = MetricsExporter(worker, elastic, feed, interval=config.metrics_export_interval)
    supervisor = Supervisor(host.ipv4, process, updates, worker, health, performance, feed,
                            cluster_manager, memory_store)

    await _record_start(elastic)                    # before any loop starts

    ticking = asyncio.create_task(tick_loop(supervisor), name="tick")
    ticking.add_done_callback(_exit_if_failed)
    background = [
        asyncio.create_task(health.run(), name="health"),
        asyncio.create_task(performance.run(), name="performance"),
        asyncio.create_task(inventory.run(), name="inventory"),
        asyncio.create_task(metrics_exporter.run(), name="metrics_exporter"),
    ]
    for task in background:
        task.add_done_callback(_log_if_failed)

    try:
        yield                                       # serving
    finally:
        # --- shutdown ---
        ticking.cancel()                                            # stop ticking
        await asyncio.gather(ticking, return_exceptions=True)
        await supervisor.shutdown()                                 # cancel the op, ~20s for finally steps
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await updates.aclose()
        await worker.aclose()
        await elastic.aclose()
        await cluster_manager.aclose()


app = FastAPI(lifespan=lifespan)
