"""Drives the TUI through Textual's test harness, against the real environment.

    ./devenv/dev test                    everything (about 25 minutes: every scenario runs)
    ./devenv/dev test -m "not scenario"  the quick ones
"""
import asyncio
import time

import psutil
import pytest
from rich.console import Console

from harness import env as envmod
from harness.scenarios import SCENARIOS, State
from harness.tui import DevenvApp


def ours() -> list[psutil.Process]:
    return [p for p in psutil.process_iter() if envmod.is_ours(p)]


def text_of(log) -> str:
    console = Console(width=200, record=True, file=open("/dev/null", "w"))
    for strip in log.lines:
        console.print(strip.text)
    return console.export_text()


async def wait(pilot, condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not after {timeout:.0f}s")
        await pilot.pause(0.5)


async def typed(pilot, line: str) -> None:
    prompt = pilot.app.query_one("#prompt")
    prompt.focus()
    prompt.value = line
    await pilot.press("enter")
    await pilot.pause(0.2)


async def quit_and_wait(pilot, app: DevenvApp) -> None:
    await pilot.press("ctrl+c")
    await wait(pilot, lambda: not app.is_running or not envmod.RUN.exists(), 90, "cleanup")


@pytest.fixture
def app():
    app = DevenvApp()
    yield app
    app.env.teardown()                         # whatever a failed test left
    assert not ours(), "processes left behind"
    assert not envmod.RUN.exists()


async def test_base_console_and_cleanup(app: DevenvApp) -> None:
    async with app.run_test(size=(180, 50)) as pilot:
        await wait(pilot, app.base_ready.is_set, 90, "base environment")
        assert app.env.is_up(), app.env_state
        await typed(pilot, "cm versions")
        await wait(pilot, lambda: "job_1" in text_of(app.query_one("#console")), 20, "cm versions")
        await typed(pilot, "cm agents")
        await wait(pilot, lambda: "no agents" in text_of(app.query_one("#console")), 20, "cm agents")
        await typed(pilot, "cm dispatch job_9 http://x")
        assert "read-only here" in text_of(app.query_one("#console"))
        await typed(pilot, "ls -la")
        assert "only cm, run and help" in text_of(app.query_one("#console"))
        assert ours(), "the base environment should be running"
        await quit_and_wait(pilot, app)
    assert not ours()
    assert not envmod.RUN.exists()


async def test_escape_stops_and_leaves_env_up(app: DevenvApp) -> None:
    async with app.run_test(size=(180, 50)) as pilot:
        await wait(pilot, app.base_ready.is_set, 90, "base environment")
        await typed(pilot, "run reattach")
        run = app.runs["reattach"]
        await wait(pilot, lambda: run.progress.step >= 2, 90, "reattach reaches step 2")
        await pilot.press("escape")
        await wait(pilot, lambda: run.progress.state is State.STOPPED, 30, "stopped")
        await typed(pilot, "cm status")
        await wait(pilot, lambda: "active" in text_of(app.query_one("#console")), 20, "cm status")
        await quit_and_wait(pilot, app)
    assert not ours()


@pytest.mark.scenario
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_scenario_passes(app: DevenvApp, name: str) -> None:
    async with app.run_test(size=(180, 50)) as pilot:
        await wait(pilot, app.base_ready.is_set, 90, "base environment")
        app.select(name)
        await pilot.pause(0.2)
        app.query_one("#scenarios").focus()
        await pilot.press("enter")
        run = app.runs[name]
        await wait(pilot, lambda: run.progress.state not in (State.IDLE, State.RUNNING), 900, name)
        transcript = "\n".join(line.plain for line in run.lines)
        assert run.progress.state is State.PASSED, f"{run.progress.error}\n\n{transcript[-4000:]}"
        await quit_and_wait(pilot, app)
