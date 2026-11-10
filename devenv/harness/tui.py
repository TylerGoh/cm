"""The devenv TUI.

Top left: the scenarios, each with its state, step and what it's waiting for.
Top right: the selected scenario's transcript.
Bottom: a console that takes read-only cm commands, `run <scenario>` and `help`.
Enter runs the highlighted scenario, Esc stops the running one, q (in the list) or Ctrl-C quits.
Quitting tears everything down first; a second Ctrl-C during that kills whatever is left at once.
"""
from __future__ import annotations

import asyncio
import shlex
import signal
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Input, ListItem, ListView, RichLog, Static

from harness import env as envmod
from harness.env import AGENT_IPS, EnvError, Environment
from harness.scenarios import SCENARIOS, Run, State, execute

READ_ONLY = {
    "status": "Is the fleet OK?",
    "agents": "One line per live agent.",
    "agent": "Everything about one agent (cm agent <ip>).",
    "versions": "Every version and its status.",
    "canary": "The running canary, as of now.",
    "incidents": "Open and recently closed incidents.",
}
HELP = """\
cm status | agents | agent <ip> | versions | canary | incidents   (add --json for raw JSON)
run <scenario>     reset to the base state and run it (or Enter in the list)
help               this
Keys: Enter runs the highlighted scenario · Esc stops it · Tab completes · ↑/↓ history
      q (in the list) or Ctrl-C quits, after cleaning everything up
Changing commands (dispatch, rollback, restart, ...) are for scenarios only; see their transcripts."""


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Prompt(Input):
    """Tab accepts the suggestion; ↑/↓ walk the history."""
    BINDINGS = [Binding("tab", "complete", show=False, priority=True),
                Binding("up", "history(-1)", show=False), Binding("down", "history(1)", show=False)]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.history: list[str] = []
        self.history_at = 0

    def action_complete(self) -> None:
        suggestion = getattr(self, "_suggestion", "")
        if suggestion and suggestion != self.value:
            self.value = suggestion
            self.cursor_position = len(self.value)

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        self.history_at = max(0, min(len(self.history), self.history_at + step))
        self.value = self.history[self.history_at] if self.history_at < len(self.history) else ""
        self.cursor_position = len(self.value)

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self.history_at = len(self.history)


class CleanupScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    CleanupScreen { align: center middle; }
    #cleanup { width: 80; height: 24; border: thick $warning; background: $surface; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        log = RichLog(id="cleanup", markup=False, wrap=True)
        log.border_title = "cleaning up… (Ctrl-C again kills everything at once)"
        yield log


class DevenvApp(App[None]):
    TITLE = "devenv"
    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #top { height: 3fr; }
    #scenarios { width: 46; border: round $primary; }
    #scenarios > ListItem { padding: 0 1; }
    #transcript { border: round $primary; }
    #bottom { height: 2fr; border: round $accent; }
    #console { height: 1fr; }
    #prompt { border: none; height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "stop", "stop scenario", priority=True),
        Binding("ctrl+c", "quit_clean", "quit", priority=True),
        Binding("ctrl+q", "quit_clean", "quit", priority=True),
        Binding("q", "quit_clean", show=False),            # only when the prompt isn't focused
    ]

    def __init__(self, environment: Environment | None = None) -> None:
        super().__init__()
        self.env = environment or Environment()
        self.env_state = "starting…"
        self.runs = {name: Run(s, self.env) for name, s in SCENARIOS.items()}
        self.scenario_task: asyncio.Task[None] | None = None
        self.running: str | None = None
        self.shown: str = next(iter(SCENARIOS))
        self.shown_lines = 0
        self.base_ready = asyncio.Event()
        self.cleaning = False

    # --- layout ---

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="top"):
            items = [ListItem(Static(id=f"label-{name}"), id=f"item-{name}") for name in SCENARIOS]
            scenarios = ListView(*items, id="scenarios")
            scenarios.border_title = "Scenarios"
            yield scenarios
            transcript = RichLog(id="transcript", markup=False, wrap=False, auto_scroll=True)
            yield transcript
        with Vertical(id="bottom"):
            yield RichLog(id="console", markup=False, wrap=False, auto_scroll=True)
            commands = [f"cm {c}" for c in READ_ONLY if c != "agent"]
            commands += [f"cm agent {ip}" for ip in AGENT_IPS]
            commands += [f"run {name}" for name in SCENARIOS] + ["help"]
            yield Prompt(id="prompt", placeholder="cm agents · run promote · help",
                         suggester=SuggestFromList(commands, case_sensitive=False))

    def on_mount(self) -> None:
        self.query_one("#bottom").border_title = "Console"
        self.console_log(Text("devenv: read-only cm, `run <scenario>` and `help`. "
                              "The base environment is starting…", style="dim"))
        self.set_interval(0.25, self.refresh_view)
        self.query_one("#prompt", Prompt).focus()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.action_quit_clean)
        except (NotImplementedError, RuntimeError):
            pass
        self.start_base()

    # --- the environment ---

    @work(exclusive=True, group="base")
    async def start_base(self) -> None:
        self.env_state = "starting…"
        try:
            await asyncio.to_thread(envmod.clear_leftovers, self.report_to_console)
            await asyncio.to_thread(self.env.start_base, self.report_to_console)
            self.env_state = "ready (empty: job_1 ACTIVE, no agents)"
        except Exception as e:
            self.env_state = "down"
            self.console_log(Text(f"the base environment didn't start:\n{e}", style="red"))
        finally:
            self.base_ready.set()

    def report_to_console(self, line: str) -> None:
        self.call_from_thread(self.console_log, Text(f"  {line}", style="dim"))

    # --- view ---

    def console_log(self, text: Text | str) -> None:
        self.query_one("#console", RichLog).write(text)

    def refresh_view(self) -> None:
        if self.running and self.runs[self.running].progress.state is State.RUNNING:
            run = self.runs[self.running]
            self.env_state = f"scenario {self.running}" + (" (resetting…)" if run.progress.step <= 1 else "")
        keys = "Enter run · Esc stop · Tab complete · Ctrl-C quit"
        self.query_one("#status", Static).update(
            Text.assemble(("env: ", "bold"), self.env_state, ("   " + keys, "dim")))
        for name, run in self.runs.items():
            self.query_one(f"#label-{name}", Static).update(self.label(name, run))
        self.refresh_transcript()

    def label(self, name: str, run: Run) -> Text:
        p = run.progress
        style = {State.PASSED: "green", State.FAILED: "red", State.RUNNING: "yellow",
                 State.STOPPED: "yellow"}.get(p.state, "")
        head = Text(f"{p.state.value} ", style=style) + Text(name, style="bold")
        total = len(run.scenario.steps)
        if p.state is State.RUNNING:
            head += Text(f"  {p.step}/{total}  {fmt_time(p.elapsed())}", style="yellow")
            detail = f"waiting: {p.waiting}" if p.waiting else p.title
        elif p.state is State.IDLE:
            detail = run.scenario.summary
        else:
            head += Text(f"  {fmt_time(p.elapsed())}", style="dim")
            detail = p.error if p.state is State.FAILED else run.scenario.summary
        return head + Text("\n  " + detail[:120], style="dim")

    def refresh_transcript(self) -> None:
        log = self.query_one("#transcript", RichLog)
        run = self.runs[self.shown]
        p = run.progress
        log.border_title = (f"{self.shown} · step {p.step}/{len(run.scenario.steps)}"
                            if p.state is not State.IDLE else f"{self.shown} · not run yet")
        if self.shown_lines > len(run.lines):          # a new run cleared it
            log.clear()
            self.shown_lines = 0
        for line in run.lines[self.shown_lines:]:
            log.write(line)
        self.shown_lines = len(run.lines)

    @on(ListView.Highlighted, "#scenarios")
    def show_scenario(self, event: ListView.Highlighted) -> None:
        if event.item is None or event.item.id is None:
            return
        name = event.item.id.removeprefix("item-")
        if name != self.shown:
            self.shown = name
            self.shown_lines = 0
            self.query_one("#transcript", RichLog).clear()
            self.refresh_transcript()

    @on(ListView.Selected, "#scenarios")
    def run_selected(self, event: ListView.Selected) -> None:
        if event.item.id:
            self.run_scenario(event.item.id.removeprefix("item-"))

    # --- running scenarios ---

    def run_scenario(self, name: str) -> None:
        if name not in SCENARIOS:
            self.console_log(Text(f"no scenario {name!r}: {', '.join(SCENARIOS)}", style="red"))
            return
        if self.scenario_task and not self.scenario_task.done():
            self.console_log(Text(f"{self.running} is running; Esc stops it first", style="yellow"))
            return
        if self.cleaning:
            return
        self.running = name
        self.select(name)
        self.console_log(Text(f"running {name}", style="cyan"))
        self.scenario_task = asyncio.create_task(self._run(name))

    async def _run(self, name: str) -> None:
        await self.base_ready.wait()
        run = self.runs[name]
        await execute(run)
        self.env_state = (f"up after {name} ({run.progress.state.name.lower()}): inspect it with cm"
                          if self.env.is_up() else "down")

    def select(self, name: str) -> None:
        listview = self.query_one("#scenarios", ListView)
        listview.index = list(SCENARIOS).index(name)

    def action_stop(self) -> None:
        if self.scenario_task and not self.scenario_task.done():
            self.scenario_task.cancel()
            self.console_log(Text(f"stopped {self.running}; the environment stays up as it is",
                                  style="yellow"))
            self.env_state = f"up after {self.running} (stopped): inspect it with cm"

    # --- console ---

    @on(Input.Submitted, "#prompt")
    async def command(self, event: Input.Submitted) -> None:
        prompt = self.query_one("#prompt", Prompt)
        line = event.value.strip()
        prompt.value = ""
        prompt.remember(line)
        if not line:
            return
        self.console_log(Text(f"> {line}", style="bold"))
        try:
            words = shlex.split(line)
        except ValueError as e:
            self.console_log(Text(str(e), style="red"))
            return
        match words:
            case ["help"]:
                self.console_log(HELP)
            case ["run", name]:
                self.run_scenario(name)
            case ["run", *_]:
                self.console_log(Text(f"run <scenario>: {', '.join(SCENARIOS)}", style="red"))
            case ["cm", *args]:
                await self.cm(args)
            case _:
                self.console_log(Text("only cm, run and help here; try help", style="red"))

    async def cm(self, args: list[str]) -> None:
        command = [a for a in args if a not in ("--json", "--help", "-h")]
        if args and not command and any(a in ("--help", "-h") for a in args):
            command = ["--help"]
        if not command or (command[0] not in READ_ONLY and command[0] != "--help"):
            names = ", ".join(READ_ONLY)
            self.console_log(Text(f"read-only here: {names}. Scenarios run the commands that change "
                                  "things; their transcripts show what cm answers.", style="yellow"))
            return
        if any(a.startswith("--daemon-url") for a in args):
            self.console_log(Text("--daemon-url is fixed to the dev daemon", style="yellow"))
            return
        if command[0] != "--help" and not self.env.is_up():
            state = "it's still starting" if not self.base_ready.is_set() else "start a scenario first"
            self.console_log(Text(f"no environment: {state}", style="yellow"))
            return
        width = max(60, self.query_one("#console", RichLog).size.width - 2)
        proc = await asyncio.create_subprocess_exec(
            *self.env.cm_argv(*args), env=self.env.cm_env(color=True, width=width),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        for text in (out, err):
            if text.strip():
                self.console_log(Text.from_ansi(text.decode().rstrip("\n")))

    # --- quitting ---

    def action_quit_clean(self) -> None:
        if self.cleaning:
            self.force_quit()
            return
        self.cleaning = True
        if self.scenario_task and not self.scenario_task.done():
            self.scenario_task.cancel()
        screen = CleanupScreen()
        self.push_screen(screen)
        self.cleanup(screen)

    @work(group="cleanup")
    async def cleanup(self, screen: CleanupScreen) -> None:
        log = screen.query_one("#cleanup", RichLog)

        def report(line: str) -> None:
            self.call_from_thread(log.write, f"✔ {line}")
        started = time.monotonic()
        try:
            await asyncio.to_thread(self.env.teardown, report)
            log.write(Text(f"all clean in {time.monotonic() - started:.1f}s", style="green"))
        except Exception as e:
            log.write(Text(f"cleanup failed: {e}; killing what's left", style="red"))
            await asyncio.to_thread(self.env.teardown, report, force=True)
        await asyncio.sleep(0.4)
        self.exit()

    def force_quit(self) -> None:
        self.env.teardown(force=True)
        self.exit()


def check_ready() -> None:
    """Fails early, before the screen takes over, when the ports are busy."""
    try:
        envmod.clear_leftovers()
        Environment().check_ports()
    except EnvError as e:
        raise SystemExit(f"devenv: {e}")
