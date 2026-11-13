import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from agent.wire import ProcessStatus
from agent.worker_client import WorkerClient

log = logging.getLogger("agent.process")

POLL_INTERVAL = 0.5                                  # seconds between checks in every wait
SYSTEMCTL_TIMEOUT = 30.0

# The unit's ActiveState values in which the worker's process exists.
ALIVE_STATES = frozenset({"active", "activating", "reloading", "refreshing", "deactivating"})

ALLOWED_TRANSITIONS: dict[ProcessStatus, frozenset[ProcessStatus]] = {
    ProcessStatus.STOPPED: frozenset({ProcessStatus.STARTING}),
    ProcessStatus.STARTING: frozenset({ProcessStatus.RUNNING, ProcessStatus.STOPPING}),
    ProcessStatus.RUNNING: frozenset({ProcessStatus.STOPPING}),
    ProcessStatus.STOPPING: frozenset({ProcessStatus.STOPPED}),
}


class SystemctlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UnitState:
    active_state: str           # systemd's ActiveState: active, inactive, failed, deactivating, ...
    result: str                 # systemd's Result: success, exit-code, signal, timeout, ...

    @property
    def alive(self) -> bool:
        return self.active_state in ALIVE_STATES


class WorkerUnit:
    """The worker's systemd unit, driven through systemctl. The agent runs as root, so it needs no
    polkit rule. The unit is installed with the box, never by the agent; it is expected to have
    Restart=no (the agent owns the crash policy) and KillSignal=SIGKILL (every stop is a kill)."""

    def __init__(self, name: str) -> None:
        self.name = name                             # WORKER_SYSTEMCTL_NAME; "worker" means worker.service

    async def state(self) -> UnitState:
        props = await self.show("ActiveState", "Result")
        return UnitState(active_state=props["ActiveState"], result=props["Result"])

    async def show(self, *properties: str) -> dict[str, str]:
        args = [arg for p in properties for arg in ("-p", p)]
        out = await self._run("show", self.name, *args)
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        missing = [p for p in properties if p not in props]
        if missing:
            raise SystemctlError(f"systemctl show {self.name} gave no {', '.join(missing)}")
        return props

    async def start(self) -> None:
        """Queues the start and returns; the caller waits for the ready line. Never waits behind
        another job queued for the unit."""
        await self._run("start", "--no-block", self.name)

    async def stop(self) -> None:
        """Queues the stop and returns; the caller waits for the unit to leave ALIVE_STATES."""
        await self._run("stop", "--no-block", self.name)

    async def kill(self) -> None:
        """SIGKILL outside a stop, so the unit ends up failed: a crash, as far as the agent is concerned."""
        await self._run("kill", "--signal=SIGKILL", self.name)

    async def reset_failed(self) -> None:
        await self._run("reset-failed", self.name)

    async def _run(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), SYSTEMCTL_TIMEOUT)
        except TimeoutError:
            raise SystemctlError(f"systemctl {' '.join(args)} took over {SYSTEMCTL_TIMEOUT:.0f}s") from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode != 0:
            raise SystemctlError(f"systemctl {' '.join(args)} exited {proc.returncode}: "
                                 f"{err.decode(errors='replace').strip()}")
        return out.decode(errors="replace")


@dataclass(frozen=True, slots=True)
class ReattachPlan:
    """What reattach found still in transition; the REATTACH guards read it."""
    wait_for_start: bool        # live worker: REATTACH runs wait_for_version
    wait_for_stop: bool         # unit deactivating: REATTACH runs the full stop


class ProcessManager:
    """Owns ProcessStatus. The worker is the systemd unit WORKER_SYSTEMCTL_NAME; whether its process
    exists is read from the unit each time. What the agent last moved it to (STOPPED, STARTING,
    RUNNING, STOPPING) is held in memory only: reattach rebuilds it from the unit after an agent
    restart. Reads the worker only through WorkerClient.version(), to tell when a reattached
    worker is ready."""

    attached: bool      # False until reattach succeeds. In memory on purpose: an agent restart
                        # resets it, so the next tick does REATTACH.

    def __init__(self, worker: WorkerClient, unit: WorkerUnit, *, program_log_path: Path,
                 initialize_flag: str, start_timeout: float, stop_timeout: float) -> None:
        self._worker = worker                        # only for the version ping in wait_for_version
        self._unit = unit
        self._program_log_path = program_log_path    # the worker's own, ever-growing log
        self._initialize_flag = initialize_flag
        self._start_timeout = start_timeout          # bounds wait_for_log and wait_for_version
        self._stop_timeout = stop_timeout            # bounds the wait for the exit after a kill
        self._state = ProcessStatus.STOPPED          # meaningful once attached; never CRASHED
        self.attached = False

    async def check_unit(self) -> None:
        """At startup. A unit systemd doesn't know fails startup. Restart and KillSignal other
        than the install script's are logged: with them the agent still runs, but systemd would
        restart a crashed worker behind its back, or a stop would wait for the worker to exit."""
        props = await self._unit.show("LoadState", "Restart", "KillSignal")
        if props["LoadState"] != "loaded":
            raise RuntimeError(f"worker unit {self._unit.name}: LoadState={props['LoadState']} "
                               "(check WORKER_SYSTEMCTL_NAME)")
        if props["Restart"] != "no":
            log.error("worker unit %s has Restart=%s; it should be Restart=no",
                      self._unit.name, props["Restart"])
        if props["KillSignal"] != "9":
            log.error("worker unit %s has KillSignal=%s; it should be KillSignal=SIGKILL",
                      self._unit.name, props["KillSignal"])

    # --- status ---

    async def derive_status(self, responsive: bool | None) -> ProcessStatus | None:
        """The status as it is now: the state the agent moved the worker to, corrected for a
        worker that is gone or hung. `responsive` is the health verdict: False means alive but hung.
        A worker running while the agent has it STOPPED was started outside the agent: that
        detaches, returning None, so the next tick does REATTACH and adopts it. So does a unit
        systemctl can't read: REATTACH then retries every tick until it can."""
        try:
            alive = (await self._unit.state()).alive
        except SystemctlError as error:
            log.error("reading worker unit %s failed, reattaching: %s", self._unit.name, error)
            self.attached = False
            return None
        match self._state:
            case ProcessStatus.STOPPING if not alive:
                return ProcessStatus.STOPPED
            case ProcessStatus.STARTING | ProcessStatus.RUNNING if not alive:
                return ProcessStatus.CRASHED         # whatever the exit code: the agent didn't stop it
            case ProcessStatus.RUNNING if responsive is False:
                return ProcessStatus.CRASHED         # alive but hung
            case ProcessStatus.STOPPED if alive:
                log.warning("worker unit %s is running, started outside the agent; reattaching",
                            self._unit.name)
                self.attached = False
                return None
            case s:
                return s

    def transition_to(self, state: ProcessStatus) -> None:
        """Moves the in-memory state along ALLOWED_TRANSITIONS. A no-op when already in `state`,
        so a retried step can ask again. CRASHED is never a key or a target."""
        if self._state == state:
            return
        if state not in ALLOWED_TRANSITIONS[self._state]:
            raise RuntimeError(f"invalid process transition {self._state} -> {state}")
        self._state = state

    # --- steps ---

    async def reattach(self) -> ReattachPlan:
        """Rebuilds the state from the unit, then attaches. A failed unit is a crash the agent
        didn't see: it's reset and counted as STOPPED, so START brings it back without spending
        the crash restart. Returns which transition a live worker is still in."""
        unit = await self._unit.state()
        if unit.active_state == "deactivating":
            self._state = ProcessStatus.STOPPING
        elif unit.alive:
            self._state = ProcessStatus.STARTING     # ready only once it answers wait_for_version
        else:
            if unit.active_state == "failed":
                log.warning("worker unit %s failed while the agent was away (%s); counted as stopped",
                            self._unit.name, unit.result)
                await self._unit.reset_failed()
            self._state = ProcessStatus.STOPPED
        self.attached = True
        return ReattachPlan(wait_for_start=self._state == ProcessStatus.STARTING,
                            wait_for_stop=self._state == ProcessStatus.STOPPING)

    async def start(self) -> int:
        """Starts the unit, entering STARTING first so a tick in between doesn't see a worker the
        agent didn't start. Doesn't wait: wait_for_log does. A start systemctl refuses leaves
        STOPPED. Returns the program log's size just before the start, so wait_for_log reads only
        this run's lines (the log is the worker's own, ever-growing file)."""
        if self._state != ProcessStatus.STOPPED:
            raise RuntimeError(f"start needs STOPPED, the agent has {self._state}")
        log_offset = self._program_log_size()        # before the start: the worker appends at once
        self.transition_to(ProcessStatus.STARTING)
        try:
            await self._unit.start()
        except SystemctlError:
            self._state = ProcessStatus.STOPPED
            raise
        log.info("started worker unit %s", self._unit.name)
        return log_offset

    async def wait_for_log(self, log_offset: int) -> None:
        """Reads the program log forward from log_offset until the ready line, then goes to RUNNING.
        The ready line comes later than the version ping and means the metrics can be trusted.
        If the worker exits first, raises at once, leaving STARTING with no process (derives
        CRASHED). On start_timeout, kills the worker and raises, with the same result."""
        if self._state == ProcessStatus.RUNNING:
            return
        if self._state != ProcessStatus.STARTING:
            raise RuntimeError(f"wait_for_log needs STARTING, the agent has {self._state}")
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
            if not (await self._unit.state()).alive:
                raise RuntimeError(f"worker unit {self._unit.name} exited before logging ready")
            if loop.time() >= deadline:
                await self._kill_after_timeout("no ready line")
            await asyncio.sleep(POLL_INTERVAL)

    async def wait_for_version(self) -> None:
        """REATTACH, live worker: ready once it answers the version endpoint, then RUNNING. Earlier
        than the log's ready line, which is accepted when reattaching. On start_timeout it kills
        the worker and raises, like wait_for_log."""
        if self._state == ProcessStatus.RUNNING:
            return
        if self._state != ProcessStatus.STARTING:
            raise RuntimeError(f"wait_for_version needs STARTING, the agent has {self._state}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._start_timeout
        while await self._worker.version() is None:
            if not (await self._unit.state()).alive:
                raise RuntimeError(f"worker unit {self._unit.name} exited before answering")
            if loop.time() >= deadline:
                await self._kill_after_timeout("no answer from the version endpoint")
            await asyncio.sleep(POLL_INTERVAL)
        self.transition_to(ProcessStatus.RUNNING)

    async def stop(self) -> None:
        """Real name: kill_instance. Goes to STOPPING, stops the unit (its KillSignal=SIGKILL makes
        every stop a kill), then waits for the exit. A no-op on STOPPED."""
        if self._state == ProcessStatus.STOPPED:
            return
        self.transition_to(ProcessStatus.STOPPING)
        await self._unit.stop()
        await self.wait_for_stop()

    # --- used by stop, not a step on its own ---

    async def wait_for_stop(self) -> None:
        """Sends nothing: waits until the unit's process is gone, then goes to STOPPED. A unit
        left failed (it had crashed before this stop) is reset, so STOPPED is always inactive.
        Raises after stop_timeout, leaving STOPPING with a live worker."""
        if self._state == ProcessStatus.STOPPED:
            return
        if not await self._wait_gone(self._stop_timeout):
            raise TimeoutError(f"worker unit {self._unit.name} still running {self._stop_timeout}s "
                               "after systemctl stop")
        if (await self._unit.state()).active_state == "failed":
            await self._unit.reset_failed()
        self.transition_to(ProcessStatus.STOPPED)

    # --- private ---

    def _is_ready_line(self, line: bytes) -> bool:
        return self._initialize_flag in line.decode(errors="replace")

    def _program_log_size(self) -> int:
        try:
            return self._program_log_path.stat().st_size
        except FileNotFoundError:
            return 0

    async def _wait_gone(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (await self._unit.state()).alive:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(POLL_INTERVAL)
        return True

    async def _kill_after_timeout(self, reason: str) -> NoReturn:
        """Shared by both waits: SIGKILL outside a stop, wait for the exit, raise. Leaves STARTING
        with a failed unit, so the status derives CRASHED."""
        await self._unit.kill()
        if not await self._wait_gone(self._stop_timeout):
            raise TimeoutError(f"{reason} within {self._start_timeout}s, and worker unit "
                               f"{self._unit.name} outlived SIGKILL")
        raise TimeoutError(f"{reason} within {self._start_timeout}s; worker killed")
