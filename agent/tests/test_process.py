"""ProcessManager against a fake unit held in memory, and WorkerUnit against a fake systemctl."""

import asyncio
import logging
import os
from pathlib import Path

import pytest

from agent import process as process_module
from agent.process import ProcessManager, SystemctlError, UnitState, WorkerUnit
from agent.wire import ProcessStatus

READY = "worker ready"


class FakeUnit:
    """A systemd unit as the agent sees it. On start the worker logs `on_start` lines to the
    program log; a stop ends inactive, a kill (or a crash) failed."""

    name = "worker.service"

    def __init__(self, program_log: Path, on_start: str | None = f"starting\n{READY}\n") -> None:
        self.program_log = program_log
        self.on_start = on_start
        self.active_state = "inactive"
        self.result = "success"
        self.props = {"LoadState": "loaded", "Restart": "no", "KillSignal": "9"}
        self.calls: list[str] = []
        self.refuse_start = False

    async def state(self) -> UnitState:
        return UnitState(self.active_state, self.result)

    async def show(self, *properties: str) -> dict[str, str]:
        return {p: self.props[p] for p in properties}

    async def start(self) -> None:
        self.calls.append("start")
        if self.refuse_start:
            raise SystemctlError("systemctl start worker.service exited 1: refused")
        self.active_state, self.result = "active", "success"
        if self.on_start is not None:
            with open(self.program_log, "a") as program_log:
                program_log.write(self.on_start)

    async def stop(self) -> None:
        self.calls.append("stop")
        if self.active_state != "failed":
            self.active_state = "inactive"

    async def kill(self) -> None:
        self.calls.append("kill")
        self.crash("signal")

    async def reset_failed(self) -> None:
        self.calls.append("reset-failed")
        self.active_state = "inactive"

    def crash(self, result: str = "exit-code") -> None:
        self.active_state, self.result = "failed", result


class FakeWorker:
    def __init__(self, version: str | None) -> None:
        self.answer = version

    async def version(self) -> str | None:
        return self.answer


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_module, "POLL_INTERVAL", 0.01)


def manager(tmp_path: Path, *, on_start: str | None = f"starting\n{READY}\n", start_timeout: float = 1,
            worker: FakeWorker | None = None) -> tuple[ProcessManager, FakeUnit]:
    unit = FakeUnit(tmp_path / "worker.log", on_start)
    pm = ProcessManager(worker or FakeWorker(None), unit,  # type: ignore[arg-type]
                        program_log_path=unit.program_log, initialize_flag=READY,
                        start_timeout=start_timeout, stop_timeout=1)
    asyncio.run(pm.reattach())
    return pm, unit


def status(pm: ProcessManager, responsive: bool | None = None) -> ProcessStatus | None:
    return asyncio.run(pm.derive_status(responsive))


def test_start_wait_for_log_stop(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)

    async def scenario() -> None:
        offset = await pm.start()
        assert await pm.derive_status(None) is ProcessStatus.STARTING
        await pm.wait_for_log(offset)
        assert await pm.derive_status(None) is ProcessStatus.RUNNING
        await pm.stop()
        assert await pm.derive_status(None) is ProcessStatus.STOPPED

    asyncio.run(scenario())
    assert unit.calls == ["start", "stop"] and unit.active_state == "inactive"


def test_old_ready_lines_before_the_offset_are_ignored(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path, on_start="starting\n")
    unit.program_log.write_text(f"{READY}\n" * 3)                    # a previous run's lines

    async def scenario() -> None:
        offset = await pm.start()
        with pytest.raises(TimeoutError, match="no ready line"):
            await pm.wait_for_log(offset)

    asyncio.run(scenario())
    assert unit.calls == ["start", "kill"] and unit.active_state == "failed"
    assert status(pm) is ProcessStatus.CRASHED                        # killed outside a stop


def test_exit_before_ready_fails_at_once_as_a_crash(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path, on_start=None, start_timeout=30)

    async def scenario() -> None:
        offset = await pm.start()
        unit.crash()
        with pytest.raises(RuntimeError, match="exited before logging ready"):
            await asyncio.wait_for(pm.wait_for_log(offset), timeout=5)

    asyncio.run(scenario())
    assert status(pm) is ProcessStatus.CRASHED


def test_hung_worker_derives_crashed(tmp_path: Path) -> None:
    pm, _ = manager(tmp_path)

    async def scenario() -> None:
        await pm.wait_for_log(await pm.start())
        assert await pm.derive_status(False) is ProcessStatus.CRASHED

    asyncio.run(scenario())


def test_a_clean_exit_the_agent_didnt_ask_for_is_a_crash(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)
    asyncio.run(pm.wait_for_log(asyncio.run(pm.start())))
    unit.active_state = "inactive"                                    # exit 0: systemd calls it success
    assert status(pm) is ProcessStatus.CRASHED


def test_stop_after_a_crash_resets_the_failed_unit(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)
    asyncio.run(pm.wait_for_log(asyncio.run(pm.start())))
    unit.crash()
    asyncio.run(pm.stop())                                            # the first step of CRASH_RESTART
    assert unit.calls == ["start", "stop", "reset-failed"]
    assert unit.active_state == "inactive" and status(pm) is ProcessStatus.STOPPED


def test_a_start_systemctl_refuses_stays_stopped(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)
    unit.refuse_start = True
    with pytest.raises(SystemctlError, match="refused"):
        asyncio.run(pm.start())
    assert status(pm) is ProcessStatus.STOPPED


def test_reattach_counts_a_failed_unit_as_stopped(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)
    unit.crash()                                                      # while a new agent starts
    plan = asyncio.run(pm.reattach())
    assert not plan.wait_for_start and not plan.wait_for_stop
    assert unit.calls == ["reset-failed"] and status(pm) is ProcessStatus.STOPPED


def test_reattach_to_a_live_worker_waits_for_the_version(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path, worker=FakeWorker("job_7"))
    unit.active_state = "active"
    plan = asyncio.run(pm.reattach())
    assert plan.wait_for_start and not plan.wait_for_stop
    assert status(pm) is ProcessStatus.STARTING
    asyncio.run(pm.wait_for_version())
    assert status(pm) is ProcessStatus.RUNNING


def test_reattach_to_a_stopping_unit_finishes_the_stop(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)
    unit.active_state = "deactivating"
    plan = asyncio.run(pm.reattach())
    assert plan.wait_for_stop and not plan.wait_for_start
    assert status(pm) is ProcessStatus.STOPPING
    asyncio.run(pm.stop())
    assert status(pm) is ProcessStatus.STOPPED


def test_a_worker_started_outside_the_agent_detaches(tmp_path: Path) -> None:
    pm, unit = manager(tmp_path)
    unit.active_state = "active"                                      # systemctl start by hand
    assert status(pm) is None and not pm.attached


def test_transitions_follow_the_table(tmp_path: Path) -> None:
    pm, _ = manager(tmp_path)
    pm.transition_to(ProcessStatus.STOPPED)                           # same state: a no-op
    with pytest.raises(RuntimeError, match="invalid process transition"):
        pm.transition_to(ProcessStatus.RUNNING)


def test_start_refuses_unless_stopped(tmp_path: Path) -> None:
    pm, _ = manager(tmp_path)
    asyncio.run(pm.start())
    with pytest.raises(RuntimeError, match="start needs STOPPED"):
        asyncio.run(pm.start())


def test_check_unit(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    pm, unit = manager(tmp_path)
    unit.props.update(Restart="on-failure", KillSignal="15")
    with caplog.at_level(logging.ERROR):
        asyncio.run(pm.check_unit())
    assert "Restart=on-failure" in caplog.text and "KillSignal=15" in caplog.text
    unit.props["LoadState"] = "not-found"
    with pytest.raises(RuntimeError, match="LoadState=not-found"):
        asyncio.run(pm.check_unit())


# --- WorkerUnit, against a systemctl stand-in on PATH ---

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$@" >> "$(dirname "$0")/calls"
case "$1" in
  show) printf 'ActiveState=failed\\nResult=signal\\nIgnored line\\n' ;;
  start) echo "Failed to start $3: Unit $3 not found." >&2; exit 5 ;;
esac
"""


@pytest.fixture
def systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "systemctl"
    script.write_text(FAKE_SYSTEMCTL)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir / "calls"


def test_worker_unit_drives_systemctl(systemctl: Path) -> None:
    unit = WorkerUnit("worker.service")

    async def scenario() -> None:
        assert await unit.state() == UnitState("failed", "signal")
        await unit.stop()
        await unit.kill()
        await unit.reset_failed()
        with pytest.raises(SystemctlError, match="exited 5: Failed to start worker.service"):
            await unit.start()
        with pytest.raises(SystemctlError, match="gave no LoadState"):
            await unit.show("LoadState")

    asyncio.run(scenario())
    assert systemctl.read_text().splitlines()[:5] == [
        "show worker.service -p ActiveState -p Result",
        "stop --no-block worker.service",
        "kill --signal=SIGKILL worker.service",
        "reset-failed worker.service",
        "start --no-block worker.service",
    ]
