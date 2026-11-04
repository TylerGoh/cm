"""ProcessManager against real processes: a small Python script stands in for the worker."""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from agent.models import ProcessMemory
from agent.process import ProcessManager
from agent.state_store import JsonStore
from agent.wire import ProcessStatus

# Appends the ready line to the program log after a short delay, then keeps running.
WORKER = """
import sys, time
time.sleep(0.3)
with open(sys.argv[1], "a") as log:
    log.write("starting\\nworker ready\\nbusy\\n")
print("console output")
time.sleep(60)
"""
EXITS_EARLY = "import sys; sys.exit(3)"


class FakeWorker:
    def __init__(self, version: str | None) -> None:
        self.answer = version

    async def version(self) -> str | None:
        return self.answer


def manager(tmp_path: Path, script: str, *, start_timeout: float = 5,
            worker: FakeWorker | None = None) -> tuple[ProcessManager, JsonStore[ProcessMemory]]:
    store = JsonStore(ProcessMemory, tmp_path / "process.json")
    store.save(ProcessMemory(pid=None, create_time=None, state=ProcessStatus.STOPPED))
    program_log = tmp_path / "worker.log"
    pm = ProcessManager(worker or FakeWorker(None), store,  # type: ignore[arg-type]
                        command=[sys.executable, "-c", script, str(program_log)],
                        console_log_dir=tmp_path / "console", program_log_path=program_log,
                        initialize_flag="worker ready", start_timeout=start_timeout, stop_timeout=5)
    return pm, store


def test_start_wait_for_log_stop(tmp_path: Path) -> None:
    pm, store = manager(tmp_path, WORKER)

    async def scenario() -> None:
        offset = await pm.start()
        assert store.load().state is ProcessStatus.STARTING
        await pm.wait_for_log(offset)
        assert pm.derive_status(None) is ProcessStatus.RUNNING
        pid = store.load().pid
        await pm.stop()
        assert store.load() == ProcessMemory(pid=None, create_time=None, state=ProcessStatus.STOPPED)
        assert pid is not None and not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE

    asyncio.run(scenario())
    consoles = list((tmp_path / "console").iterdir())
    assert len(consoles) == 1 and consoles[0].name.startswith("worker-console-")


def test_old_ready_lines_before_the_offset_are_ignored(tmp_path: Path) -> None:
    pm, store = manager(tmp_path, "import time; time.sleep(60)", start_timeout=1)
    (tmp_path / "worker.log").write_text("worker ready\n" * 3)      # a previous run's lines

    async def scenario() -> None:
        offset = await pm.start()
        with pytest.raises(TimeoutError, match="no ready line"):
            await pm.wait_for_log(offset)

    asyncio.run(scenario())
    assert pm.derive_status(None) is ProcessStatus.CRASHED          # killed, file left at STARTING


def test_exit_before_ready_fails_at_once_as_a_crash(tmp_path: Path) -> None:
    pm, _ = manager(tmp_path, EXITS_EARLY, start_timeout=30)

    async def scenario() -> None:
        offset = await pm.start()
        with pytest.raises(RuntimeError, match="exited before logging ready"):
            await asyncio.wait_for(pm.wait_for_log(offset), timeout=5)

    asyncio.run(scenario())
    assert pm.derive_status(None) is ProcessStatus.CRASHED


def test_hung_worker_derives_crashed(tmp_path: Path) -> None:
    pm, _ = manager(tmp_path, WORKER)

    async def scenario() -> None:
        await pm.wait_for_log(await pm.start())
        assert pm.derive_status(False) is ProcessStatus.CRASHED
        await pm.stop()

    asyncio.run(scenario())


def test_reattach_settles_a_dead_pid_to_stopped(tmp_path: Path) -> None:
    pm, store = manager(tmp_path, WORKER)
    store.save(ProcessMemory(pid=999_999, create_time=datetime.now(UTC), state=ProcessStatus.RUNNING))
    plan = asyncio.run(pm.reattach())
    assert plan.wait_for_start is False and plan.wait_for_stop is False
    assert store.load().state is ProcessStatus.STOPPED and pm.attached


def test_reattach_to_a_live_start_waits_for_the_version(tmp_path: Path) -> None:
    worker = FakeWorker("job_7")
    pm, store = manager(tmp_path, "import time; time.sleep(60)", worker=worker)

    async def scenario() -> None:
        await pm.start()                             # as if the previous agent died here
        fresh = ProcessManager(worker, store,  # type: ignore[arg-type]       # a new agent, same file
                               command=["unused"], console_log_dir=tmp_path / "console",
                               program_log_path=tmp_path / "worker.log", initialize_flag="worker ready",
                               start_timeout=5, stop_timeout=5)
        plan = await fresh.reattach()
        assert plan.wait_for_start
        await fresh.wait_for_version()
        assert store.load().state is ProcessStatus.RUNNING
        await fresh.stop()

    asyncio.run(scenario())


def test_transitions_follow_the_table(tmp_path: Path) -> None:
    pm, store = manager(tmp_path, WORKER)
    pm.transition_to(ProcessStatus.STOPPED)          # same state: a no-op
    with pytest.raises(RuntimeError, match="invalid process transition"):
        pm.transition_to(ProcessStatus.RUNNING)
    with pytest.raises(ValueError, match="needs pid"):
        pm.transition_to(ProcessStatus.STARTING)
    assert store.load().state is ProcessStatus.STOPPED


def test_start_refuses_unless_stopped(tmp_path: Path) -> None:
    pm, store = manager(tmp_path, WORKER)
    store.save(ProcessMemory(pid=1, create_time=datetime.now(UTC), state=ProcessStatus.RUNNING))
    with pytest.raises(RuntimeError, match="start needs STOPPED"):
        asyncio.run(pm.start())
