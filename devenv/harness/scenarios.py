"""The scenarios, and the small engine they run on.

A scenario is an async function over a Run. A Run writes the transcript (every command that changes
something, shown with the exact `cm ...` line and cm's full answer as a terminal would show it),
and checks the daemon through `cm --json`. Every check waits until it holds or its deadline
passes; on a timeout the failure says what was last seen.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from rich.text import Text

from harness.env import AGENT_IPS, UPSTREAM_URL, Environment


class Failed(Exception):
    """A check that didn't hold. The message says what was expected and what was seen."""


class State(Enum):
    IDLE = "·"
    RUNNING = "▶"
    PASSED = "✔"
    FAILED = "✘"
    STOPPED = "■"


@dataclass
class Progress:
    state: State = State.IDLE
    step: int = 0
    title: str = ""
    waiting: str = ""
    started: float | None = None
    finished: float | None = None
    error: str = ""

    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or time.monotonic()) - self.started


Line = Text                                     # one transcript entry
Changed = Callable[[], None]


@dataclass
class Run:
    """One run of one scenario. The TUI reads `progress` and `lines`; `changed` tells it to redraw."""
    scenario: Scenario
    env: Environment
    changed: Changed = lambda: None
    progress: Progress = field(default_factory=Progress)
    lines: list[Line] = field(default_factory=list)

    # --- transcript ---

    def say(self, text: str | Text, style: str = "") -> None:
        self.lines.append(text if isinstance(text, Text) else Text(text, style=style))
        self.changed()

    def step(self, title: str) -> None:
        self.progress.step += 1
        self.progress.title = title
        self.progress.waiting = ""
        self.say(Text(f"\n── {self.progress.step}/{len(self.scenario.steps)} {title}", style="bold cyan"))

    def ok(self, text: str) -> None:
        self.say(Text("✔ ", style="green") + Text(text))

    # --- cm ---

    async def _cm(self, *args: str, color: bool) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *self.env.cm_argv(*args), env=self.env.cm_env(color=color),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(), err.decode()

    async def cm(self, *args: str, expect_fail: bool = False) -> str:
        """Runs `cm args` as you would, and puts the line and its answer in the transcript."""
        self.say(Text(f"$ cm {' '.join(args)}", style="bold"))
        code, out, err = await self._cm(*args, color=True)
        for text in (out, err):
            if text.strip():
                self.say(Text.from_ansi(text.rstrip("\n")))
        if expect_fail and code == 0:
            raise Failed(f"cm {' '.join(args)} was expected to be refused, but succeeded")
        if not expect_fail and code != 0:
            raise Failed(f"cm {' '.join(args)} failed (exit {code})")
        return out + err

    async def query(self, *args: str) -> dict:
        """cm --json <args>, not shown: what the checks read."""
        code, out, err = await self._cm("--json", *args, color=False)
        if code != 0:
            raise Failed(f"cm --json {' '.join(args)} failed (exit {code}): {err.strip()[:200]}")
        return json.loads(out)

    # --- waiting ---

    async def until(self, what: str, check: Callable[[], Awaitable[tuple[bool, str]]],
                    timeout: float, every: float = 2.0) -> str:
        """Polls check() until it returns (True, seen) or timeout passes. Shows the wait as it goes."""
        start = time.monotonic()
        seen = "nothing yet"
        while True:
            waited = time.monotonic() - start
            self.progress.waiting = f"{what} ({waited:.0f}s/{timeout:.0f}s)"
            self.changed()
            try:
                done, seen = await check()
            except Failed as e:
                done, seen = False, str(e)
            if done:
                self.progress.waiting = ""
                self.ok(f"{what}  [{waited:.0f}s]")
                return seen
            if waited >= timeout:
                raise Failed(f"{what}: gave up after {timeout:.0f}s; last seen: {seen}")
            await asyncio.sleep(every)

    # --- the fleet, read through cm --json ---

    async def agents(self) -> dict[str, dict]:
        return {a["ip"]: a for a in (await self.query("agents"))["agents"]}

    async def versions(self) -> dict[str, dict]:
        return {v["version"]: v for v in (await self.query("versions"))["versions"]}

    async def wait_converged(self, version: str, ips: tuple[str, ...] = AGENT_IPS,
                             timeout: float = 150) -> None:
        async def check() -> tuple[bool, str]:
            rows = await self.agents()
            seen = ", ".join(f"{ip} {r['condition']} on {r['running_version']}" for ip, r in rows.items())
            done = all(ip in rows and rows[ip]["condition"] == "converged"
                       and rows[ip]["running_version"] == version for ip in ips)
            return done, seen or "no agents"
        await self.until(f"{', '.join(ips)} converged on {version}", check, timeout)

    async def wait_version(self, version: str, status: str, timeout: float = 200,
                           rollback: bool | None = None) -> dict:
        async def check() -> tuple[bool, str]:
            row = (await self.versions()).get(version)
            if row is None:
                return False, f"{version} not listed"
            done = row["status"] == status and (rollback is None or row["activated_by_rollback"] == rollback)
            return done, f"{version} is {row['status']}"
        await self.until(f"{version} is {status.upper()}", check, timeout)
        return (await self.versions())[version]

    # --- the fake workers and agents ---

    async def worker(self, n: int, note: str, **settings: object) -> dict:
        self.say(Text(f"[worker {n}] {note}", style="magenta"))
        return await asyncio.to_thread(self.env.worker_control, n, **settings)

    def worker_pid(self, n: int) -> int | None:
        return self.env.worker_pid(n)

    async def last_operation(self, ip: str, op_type: str) -> dict | None:
        detail = await self.query("agent", ip)
        for op in detail["recent_operations"]:
            if op["type"] == op_type:
                return op
        return None


@dataclass
class Scenario:
    name: str
    summary: str
    steps: list[str]
    body: Callable[[Run], Awaitable[None]]


SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str, summary: str, steps: list[str]):
    def register(body: Callable[[Run], Awaitable[None]]) -> Callable[[Run], Awaitable[None]]:
        SCENARIOS[name] = Scenario(name, summary, steps, body)
        return body
    return register


async def execute(run: Run) -> None:
    """Resets to the base state, then runs the scenario. Leaves the environment up afterwards."""
    p = run.progress
    p.state, p.started, p.finished, p.step, p.error = State.RUNNING, time.monotonic(), None, 0, ""
    run.lines.clear()
    try:
        run.step("reset to base: empty daemon with job_1")
        await asyncio.to_thread(run.env.teardown, lambda line: None)
        await asyncio.to_thread(run.env.start_base, lambda line: run.say(Text(f"  {line}", style="dim")))
        await run.cm("versions")
        await run.cm("agents")
        await run.scenario.body(run)
        p.state = State.PASSED
        run.say(Text("\nPASSED", style="bold green"))
    except asyncio.CancelledError:
        p.state = State.STOPPED
        run.say(Text("\nSTOPPED (Esc). The environment stays up as it is.", style="bold yellow"))
        raise
    except Exception as e:                      # Failed, or the environment itself broke
        p.state = State.FAILED
        p.error = str(e)
        run.say(Text(f"\nFAILED: {e}", style="bold red"))
        if not isinstance(e, Failed):
            run.say(Text(f"({type(e).__name__})", style="red"))
        run.say(Text("The environment stays up: inspect it with cm, or see devenv/.run/logs/.", style="dim"))
    finally:
        p.finished = time.monotonic()
        p.waiting = ""
        run.changed()


# --- shared steps ---

async def bring_up_agents(run: Run) -> None:
    run.step("start two agents; each spawns a fake worker")
    for n in (1, 2):
        await asyncio.to_thread(run.env.start_agent, n, lambda line: run.say(Text(f"  {line}", style="dim")))
    run.say(Text("  the agents register, start their workers (version 'factory'), then update to job_1",
                 style="dim"))
    await run.wait_converged("job_1", timeout=150)
    await run.cm("agents")


async def promote(run: Run, version: str) -> None:
    """Dispatch a good version and wait until the whole fleet runs it."""
    run.step(f"dispatch {version}: canary on one agent")
    await run.cm("dispatch", version, f"{UPSTREAM_URL}/jobs/{version}")
    await run.cm("canary")
    await run.cm("agents", "--canary")
    canary_ips = [a["ip"] for a in (await run.query("canary"))["canary"]["agents"]]
    marked = [ip for ip, a in (await run.agents()).items() if a["canary"]]
    if marked != canary_ips:
        raise Failed(f"cm agents marks {marked} as canary, but the canary is {canary_ips}")
    run.ok(f"cm agents marks only the canary agent ({', '.join(marked)})")
    run.step(f"wait for the canary deadline: {version} promoted")
    await run.wait_version(version, "active", timeout=240)
    await run.cm("versions")
    await run.wait_converged(version, timeout=150)
    await run.cm("agents")
    if any(a["canary"] for a in (await run.agents()).values()):
        raise Failed("cm agents still marks an agent as canary after the promotion")


# --- the scenarios ---

@scenario("promote", "a good version passes its canary and reaches the fleet", [
    "reset", "agents up", "dispatch", "promoted", "status"])
async def _promote(run: Run) -> None:
    await bring_up_agents(run)
    await promote(run, "job_2")
    run.step("the fleet as cm status sees it")
    await run.cm("status")


@scenario("canary-rejected", "a version with bad numbers fails its canary and is marked UNSTABLE", [
    "reset", "agents up", "dispatch", "violation", "rejected", "canary agent back"])
async def _canary_rejected(run: Run) -> None:
    await bring_up_agents(run)
    run.step("dispatch job_2_bad (its workers report bad numbers)")
    await run.cm("dispatch", "job_2_bad", f"{UPSTREAM_URL}/jobs/job_2_bad")
    canary = (await run.query("canary"))["canary"]
    ip = canary["agents"][0]["ip"]
    run.say(Text(f"  canary agent: {ip}", style="dim"))

    run.step("the canary agent reports a performance violation")

    async def violating() -> tuple[bool, str]:
        row = (await run.agents())[ip]
        return bool(row["violations"]), f"{row['condition']} on {row['running_version']}, violations {row['violations']}"
    await run.until(f"{ip} reports a violation", violating, timeout=150)
    await run.cm("agent", ip)
    await run.cm("canary")

    run.step("at the deadline the canary is rejected")
    row = await run.wait_version("job_2_bad", "unstable", timeout=200)
    await run.cm("versions")
    if not row["unstable_reason"]:
        raise Failed("job_2_bad is UNSTABLE but has no unstable_reason")
    run.ok(f"unstable_reason: {row['unstable_reason']}")

    run.step("the canary agent goes back to job_1")
    await run.wait_converged("job_1", timeout=150)
    await run.cm("agents")


@scenario("fleet-rollback", "the ACTIVE fleet turns bad and the fleet watch rolls back", [
    "reset", "agents up", "dispatch", "promoted", "bad numbers", "violations", "rolled back"])
async def _fleet_rollback(run: Run) -> None:
    await bring_up_agents(run)
    await promote(run, "job_2")
    run.step("both workers start reporting bad numbers")
    for n in (1, 2):
        await run.worker(n, "bad numbers on", bad=True)

    run.step("the agents report performance violations")

    async def violating() -> tuple[bool, str]:
        rows = await run.agents()
        seen = "; ".join(f"{ip}: {r['violations']}" for ip, r in rows.items())
        return all(r["violations"] for r in rows.values()), seen
    await run.until("both agents report a violation", violating, timeout=120)
    await run.cm("agents")
    await run.cm("agent", AGENT_IPS[0])

    run.step("the fleet watch rolls back to job_1")
    await run.wait_version("job_1", "active", timeout=120, rollback=True)
    row = (await run.versions())["job_2"]
    if row["status"] != "unstable":
        raise Failed(f"job_2 should be UNSTABLE after the rollback, is {row['status']}")
    await run.cm("versions")
    run.ok(f"job_2 unstable_reason: {row['unstable_reason']}")
    await run.wait_converged("job_1", timeout=150)
    await run.cm("status")


@scenario("crash-restart", "a worker crashes and its agent restarts it", [
    "reset", "agents up", "crash", "crash restart"])
async def _crash_restart(run: Run) -> None:
    await bring_up_agents(run)
    run.step("worker 1 crashes")
    before = run.worker_pid(1)
    run.say(Text(f"  worker 1 pid before: {before}", style="dim"))
    await run.worker(1, "crash (exits at once)", crash=True)

    run.step("agent 1 notices and does a CRASH_RESTART")
    ip = AGENT_IPS[0]

    async def restarted() -> tuple[bool, str]:
        op = await run.last_operation(ip, "crash_restart")
        pid = run.worker_pid(1)
        rows = await run.agents()
        seen = f"{rows[ip]['condition']}, crash_restart {'none' if op is None else op['finished_at'] and 'done'}, pid {pid}"
        done = (op is not None and op["finished_at"] is not None and op["failure"] is None
                and pid is not None and pid != before and rows[ip]["condition"] == "converged")
        return done, seen
    await run.until("crash_restart finished and worker 1 has a new pid", restarted, timeout=90)
    run.ok(f"worker 1 pid now {run.worker_pid(1)} (was {before})")
    await run.cm("agent", ip)


@scenario("hang", "a worker stops answering and its agent treats it as crashed", [
    "reset", "agents up", "hang", "crash restart"])
async def _hang(run: Run) -> None:
    await bring_up_agents(run)
    run.step("worker 1 hangs: show_version and counters stall past the 2s timeout")
    before = run.worker_pid(1)
    await run.worker(1, "hang on", hang=True)

    run.step("agent 1 fails its liveness checks, then does a CRASH_RESTART")
    ip = AGENT_IPS[0]

    async def restarted() -> tuple[bool, str]:
        op = await run.last_operation(ip, "crash_restart")
        pid = run.worker_pid(1)
        row = (await run.agents())[ip]
        seen = f"{row['condition']}, healthy {row['healthy']}, crash_restart {'none' if op is None else 'seen'}, pid {pid}"
        done = (op is not None and op["finished_at"] is not None and op["failure"] is None
                and pid not in (None, before) and row["condition"] == "converged")
        return done, seen
    await run.until("crash_restart finished and worker 1 answers again", restarted, timeout=120)
    run.ok(f"worker 1 pid now {run.worker_pid(1)} (was {before}, killed by the agent)")
    await run.cm("agent", ip)


@scenario("reattach", "an agent restarts and reattaches to the worker it left running", [
    "reset", "agents up", "agent stops", "agent starts", "reattached"])
async def _reattach(run: Run) -> None:
    await bring_up_agents(run)
    ip = AGENT_IPS[0]
    before = run.worker_pid(1)
    run.step("agent 1 stops (SIGTERM, as systemd would); its worker keeps running")
    await asyncio.to_thread(run.env.stop_agent, 1, lambda line: run.say(Text(f"  {line}", style="dim")))
    if run.worker_pid(1) != before:
        raise Failed(f"worker 1 should outlive its agent: pid was {before}, now {run.worker_pid(1)}")
    run.ok(f"worker 1 still running (pid {before})")

    run.step("agent 1 starts again")
    await asyncio.to_thread(run.env.start_agent, 1, lambda line: run.say(Text(f"  {line}", style="dim")))

    run.step("agent 1 reattaches to the same worker")
    async def reattached() -> tuple[bool, str]:
        detail = await run.query("agent", ip)
        ops = detail["recent_operations"]
        latest = ops[0] if ops else None
        pid = run.worker_pid(1)
        seen = f"latest op {latest and latest['type']}, pid {pid}, {detail['agent']['condition']}"
        done = (latest is not None and latest["type"] == "reattach" and latest["failure"] is None
                and pid == before and detail["agent"]["condition"] == "converged")
        return done, seen
    await run.until(f"reattach finished with worker pid {before} unchanged", reattached, timeout=60)
    await run.cm("agent", ip)


@scenario("manual-rollback", "cm rollback: refused, accepted, then refused because rollbacks never chain", [
    "reset", "agents up", "refused", "dispatch", "promoted", "accepted", "rolled back", "never chains"])
async def _manual_rollback(run: Run) -> None:
    await bring_up_agents(run)
    run.step("cm rollback with no STABLE version: refused")
    await run.cm("rollback", expect_fail=True)
    await promote(run, "job_2")
    run.step("cm rollback after a promotion: accepted")
    await run.cm("rollback")
    await run.wait_version("job_1", "active", timeout=30, rollback=True)
    await run.cm("versions")
    run.step("the fleet goes back to job_1")
    await run.wait_converged("job_1", timeout=150)
    await run.cm("agents")
    run.step("cm rollback again: refused, rollbacks never chain")
    await run.cm("rollback", expect_fail=True)

