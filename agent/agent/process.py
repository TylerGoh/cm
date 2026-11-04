import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import psutil

from agent.models import ProcessMemory
from agent.state_store import JsonStore
from agent.wire import ProcessStatus
from agent.worker_client import WorkerClient

log = logging.getLogger("agent.process")

POLL_INTERVAL = 0.2                                  # seconds between checks in every wait
CREATE_TIME_TOLERANCE = timedelta(seconds=1)

ALLOWED_TRANSITIONS: dict[ProcessStatus, frozenset[ProcessStatus]] = {
    ProcessStatus.STOPPED: frozenset({ProcessStatus.STARTING}),
    ProcessStatus.STARTING: frozenset({ProcessStatus.RUNNING, ProcessStatus.STOPPING}),
    ProcessStatus.RUNNING: frozenset({ProcessStatus.STOPPING}),
    ProcessStatus.STOPPING: frozenset({ProcessStatus.STOPPED}),
}


@dataclass(frozen=True, slots=True)
class ReattachPlan:
    """What reattach found still in transition; the REATTACH guards read it."""
    wait_for_start: bool        # live worker in STARTING: REATTACH runs wait_for_version
    wait_for_stop: bool         # live worker in STOPPING: REATTACH runs the full stop


def _reap(pid: int) -> None:
    """Collects a dead child's exit status so it doesn't stay a zombie. A worker found by
    reattach isn't this agent's child; init reaps it, and waitpid raises ChildProcessError."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


class ProcessManager:
    """Owns ProcessStatus: writes the stored states through transition_to and derives the current
    one. Holds no status in memory; every call reloads the file. Reads the worker only through
    WorkerClient.version(), to tell when a reattached start is ready."""

    attached: bool      # False until reattach succeeds; only reattach sets it. In memory on purpose:
                        # an agent restart resets it, so the next tick does REATTACH.

    def __init__(self, worker: WorkerClient, store: JsonStore[ProcessMemory], *,
                 command: Sequence[str], console_log_dir: Path, program_log_path: Path,
                 initialize_flag: str, start_timeout: float, stop_timeout: float) -> None:
        self._worker = worker                        # only for the version ping in wait_for_version
        self._store = store
        self._command = list(command)
        self._console_log_dir = console_log_dir      # stdout/stderr, one file per spawn named by its time
        self._program_log_path = program_log_path    # the worker's own, ever-growing log
        self._initialize_flag = initialize_flag
        self._start_timeout = start_timeout          # bounds wait_for_log and wait_for_version
        self._stop_timeout = stop_timeout            # bounds the wait for the exit after a kill
        self.attached = False

    # --- status ---

    def derive_status(self, responsive: bool | None) -> ProcessStatus:
        """The status as it is now: the stored state, corrected for a dead pid or a hung worker.
        Writes nothing. `responsive` is the health verdict: False means alive but hung."""
        state = self._store.load()
        alive = self._is_alive(state)
        match state.state:
            case ProcessStatus.STOPPING if not alive:
                return ProcessStatus.STOPPED
            case ProcessStatus.STARTING | ProcessStatus.RUNNING if not alive:
                return ProcessStatus.CRASHED
            case ProcessStatus.RUNNING if responsive is False:
                return ProcessStatus.CRASHED         # alive but hung
            case s:
                return s

    def transition_to(self, state: ProcessStatus, *, pid: int | None = None,
                      create_time: datetime | None = None) -> None:
        """The only writer of the stored state. A no-op when already in `state`, so a retried
        step can ask again. CRASHED is never a key or a target, so it can't be written."""
        current = self._store.load()
        if current.state == state:
            return
        if state not in ALLOWED_TRANSITIONS[current.state]:
            raise RuntimeError(f"invalid process transition {current.state} -> {state}")
        if state != ProcessStatus.STARTING and (pid is not None or create_time is not None):
            raise ValueError("pid and create_time are only recorded when entering STARTING")
        match state:
            case ProcessStatus.STARTING:
                if pid is None or create_time is None:
                    raise ValueError("entering STARTING needs pid and create_time")
                new = ProcessMemory(pid=pid, create_time=create_time, state=state)
            case ProcessStatus.STOPPED:
                new = ProcessMemory(pid=None, create_time=None, state=state)
            case _:
                new = ProcessMemory(pid=current.pid, create_time=current.create_time, state=state)
        self._store.save(new)

    # --- steps ---

    async def reattach(self) -> ReattachPlan:
        """Settles a dead pid to STOPPED (we didn't see it die, so it isn't a crash), then attaches.
        Returns which transition a live worker is still in."""
        memory = self._store.load()
        if self._is_alive(memory):
            plan = ReattachPlan(
                wait_for_start=memory.state == ProcessStatus.STARTING,
                wait_for_stop=memory.state == ProcessStatus.STOPPING,
            )
        else:
            match memory.state:
                case ProcessStatus.STARTING | ProcessStatus.RUNNING:
                    self.transition_to(ProcessStatus.STOPPING)
                    self.transition_to(ProcessStatus.STOPPED)
                case ProcessStatus.STOPPING:
                    self.transition_to(ProcessStatus.STOPPED)
                case ProcessStatus.STOPPED:
                    pass
            plan = ReattachPlan(wait_for_start=False, wait_for_stop=False)
        self.attached = True
        return plan

    async def start(self) -> int:
        """Real name: spawn_process. Spawns the worker and goes to STARTING with its identity.
        No await between the spawn and the write. Doesn't wait: wait_for_log does.
        Returns the program log's size just before the spawn, so wait_for_log reads only this
        run's lines (the log is the worker's own, ever-growing file)."""
        memory = self._store.load()
        if memory.state != ProcessStatus.STOPPED:
            raise RuntimeError(f"start needs STOPPED, the file says {memory.state}")
        self._console_log_dir.mkdir(parents=True, exist_ok=True)
        console_log_path = self._console_log_dir / f"worker-console-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log"
        log_offset = self._program_log_size()        # before the spawn: the worker appends at once
        pid = self._spawn(console_log_path)
        create_time = datetime.fromtimestamp(psutil.Process(pid).create_time(), UTC)
        self.transition_to(ProcessStatus.STARTING, pid=pid, create_time=create_time)
        log.info("spawned worker pid %d, console log %s", pid, console_log_path)
        return log_offset

    async def wait_for_log(self, log_offset: int) -> None:
        """Reads the program log forward from log_offset until the ready line, then goes to RUNNING.
        The ready line comes later than the version ping and means the metrics can be trusted.
        If the worker exits first, raises at once, leaving STARTING with a dead pid (derives
        CRASHED). On start_timeout, kills the worker and raises, with the same result."""
        memory = self._store.load()
        if memory.state == ProcessStatus.RUNNING:
            return
        if memory.state != ProcessStatus.STARTING:
            raise RuntimeError(f"wait_for_log needs STARTING, the file says {memory.state}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._start_timeout
        offset = log_offset
        while True:
            if self._program_log_size() < offset:
                offset = 0                           # replaced or truncated: read from the start
            try:
                with open(self._program_log_path, "rb") as program_log:
                    program_log.seek(offset)
                    while (line := program_log.readline()).endswith(b"\n"):
                        offset += len(line)          # a half-written line is reread whole next poll
                        if self._is_ready_line(line):
                            self.transition_to(ProcessStatus.RUNNING)
                            return
            except FileNotFoundError:
                pass                                 # the worker hasn't created it yet
            if not self._is_alive(memory):
                raise RuntimeError(f"worker pid {memory.pid} exited before logging ready")
            if loop.time() >= deadline:
                await self._kill_after_timeout(memory, "no ready line")
            await asyncio.sleep(POLL_INTERVAL)

    async def wait_for_version(self) -> None:
        """REATTACH, live worker in STARTING: ready once it answers the version endpoint, then
        RUNNING. Earlier than the log's ready line, which is accepted when reattaching. On
        start_timeout it kills the worker and raises, like wait_for_log."""
        memory = self._store.load()
        if memory.state == ProcessStatus.RUNNING:
            return
        if memory.state != ProcessStatus.STARTING:
            raise RuntimeError(f"wait_for_version needs STARTING, the file says {memory.state}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._start_timeout
        while await self._worker.version() is None:
            if not self._is_alive(memory):
                raise RuntimeError(f"worker pid {memory.pid} exited before answering")
            if loop.time() >= deadline:
                await self._kill_after_timeout(memory, "no answer from the version endpoint")
            await asyncio.sleep(POLL_INTERVAL)
        self.transition_to(ProcessStatus.RUNNING)

    async def stop(self) -> None:
        """Real name: kill_instance. Goes to STOPPING, SIGKILLs the recorded process if alive, then
        waits for the exit. Every stop is a kill. A no-op on STOPPED."""
        memory = self._store.load()
        if memory.state == ProcessStatus.STOPPED:
            return
        self.transition_to(ProcessStatus.STOPPING)
        self._kill(memory)
        await self.wait_for_stop()

    # --- used by stop, not a step on its own ---

    async def wait_for_stop(self) -> None:
        """Sends nothing: waits until the recorded process is gone, then goes to STOPPED.
        Raises after stop_timeout, leaving STOPPING with a live pid."""
        memory = self._store.load()
        if memory.state == ProcessStatus.STOPPED:
            return
        if not await self._wait_gone(memory, self._stop_timeout):
            raise TimeoutError(f"pid {memory.pid} still alive {self._stop_timeout}s after SIGKILL")
        self.transition_to(ProcessStatus.STOPPED)

    # --- private ---

    def _spawn(self, console_log_path: Path) -> int:
        """The configured command in its own process group (so a signal to the agent's group, such
        as Ctrl-C in a terminal, doesn't reach it), stdin from /dev/null, stdout and stderr to the
        console log. The worker writes its own log to program_log_path by itself."""
        console_fd = os.open(console_log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            return os.posix_spawnp(
                self._command[0], self._command, os.environ,
                file_actions=[
                    (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                    (os.POSIX_SPAWN_DUP2, console_fd, 1),
                    (os.POSIX_SPAWN_DUP2, console_fd, 2),
                ],
                setpgroup=0,
            )
        finally:
            os.close(console_fd)

    def _is_ready_line(self, line: bytes) -> bool:
        return self._initialize_flag in line.decode(errors="replace")

    def _program_log_size(self) -> int:
        try:
            return self._program_log_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _find(self, memory: ProcessMemory) -> psutil.Process | None:
        """The live process the memory records, or None. A pid reused by another process (create_time
        differs) or a zombie counts as gone; a zombie of ours is reaped here."""
        if memory.pid is None or memory.create_time is None:
            return None
        try:
            proc = psutil.Process(memory.pid)
            created = datetime.fromtimestamp(proc.create_time(), UTC)
            if abs(created - memory.create_time) > CREATE_TIME_TOLERANCE:
                return None
            if proc.status() == psutil.STATUS_ZOMBIE:
                _reap(memory.pid)
                return None
        except psutil.ZombieProcess:
            _reap(memory.pid)
            return None
        except psutil.NoSuchProcess:
            return None
        return proc

    def _is_alive(self, memory: ProcessMemory) -> bool:
        return self._find(memory) is not None

    def _kill(self, memory: ProcessMemory) -> None:
        proc = self._find(memory)
        if proc is None:
            return
        try:
            proc.kill()                              # psutil re-checks identity before signalling
        except psutil.NoSuchProcess:
            pass

    async def _wait_gone(self, memory: ProcessMemory, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._is_alive(memory):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(POLL_INTERVAL)
        return True

    async def _kill_after_timeout(self, memory: ProcessMemory, reason: str) -> NoReturn:
        """Shared by both waits: SIGKILL, wait for the exit, raise. Leaves STARTING with a dead pid."""
        self._kill(memory)
        if not await self._wait_gone(memory, self._stop_timeout):
            raise TimeoutError(f"{reason} within {self._start_timeout}s, and pid {memory.pid} outlived SIGKILL")
        raise TimeoutError(f"{reason} within {self._start_timeout}s; worker killed")
