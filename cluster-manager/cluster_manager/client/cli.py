"""cm: the cluster manager CLI, and the command that runs the daemon (`cm daemon`).

Every other command is one request to the daemon. The daemon decides everything (conditions,
canary outcomes); this file only renders, with Rich. --json prints the response model as-is, for
scripts. `cm --help` lists the commands, and each command's docstring is its help text."""

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from dotenv import dotenv_values
from pydantic import BaseModel
from rich import box
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from cluster_manager.client.daemon_client import DaemonClient, DaemonError
from cluster_manager.daemon.wire import OperationView, ProcessStatus, StepView
from cluster_manager.schemas import (
    AgentCondition, AgentDetailResponse, AgentListResponse, AgentRow, CanaryOutcome, CanaryResponse,
    DecommissionResponse, DesiredStateChangeResponse, DispatchResponse, IncidentListResponse,
    IncidentRow, RollbackResponse, StatusResponse, VersionListResponse,
)

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"     # the daemon's settings, as in daemon/config.py
FALLBACK_PORT = "8000"


def default_daemon_url() -> str:
    """This box's daemon: PORT from the project's .env when there is one (the daemon's box),
    8000 otherwise. CM_DAEMON_URL or --daemon-url overrides it. Only PORT is read, so a .env the
    daemon would reject doesn't stop cm."""
    port = dotenv_values(ENV_FILE).get("PORT") if ENV_FILE.exists() else None
    return f"http://127.0.0.1:{port or FALLBACK_PORT}"

# markup=False: daemon text (errors, violations) is printed as-is, never read as Rich markup.
# highlight=False: only the styles below colour anything.
# Piped output (cm agents | grep x) gets room for every row on one line; Rich would otherwise
# assume 80 columns and wrap rows. A terminal uses its own width.
PIPED_WIDTH = 250
console = Console(markup=False, highlight=False, width=None if sys.stdout.isatty() else PIPED_WIDTH)
error_console = Console(stderr=True, markup=False, highlight=False)

CONDITION_STYLES: dict[AgentCondition, str] = {
    AgentCondition.NON_RESPONDING: "yellow",
    AgentCondition.CRASHED_HOLD: "bold red",
    AgentCondition.FAILED: "red",
    AgentCondition.UPDATING: "cyan",
    AgentCondition.PENDING: "cyan",
    AgentCondition.CRASHED: "red",
    AgentCondition.STOPPED: "dim",
    AgentCondition.CONVERGED: "green",
    AgentCondition.DRIFTING: "yellow",
}

OUTCOME_STYLES: dict[CanaryOutcome, str] = {
    CanaryOutcome.FAULTY_UPDATE: "bold red",
    CanaryOutcome.CRASHED: "red",
    CanaryOutcome.NOT_CONVERGED: "yellow",
    CanaryOutcome.NO_VERDICT: "dim",
    CanaryOutcome.PERFORMANCE: "red",
    CanaryOutcome.PASSED: "green",
}


# --- formatting helpers ---

def ago(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 0:
        return "now"
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def until(moment: datetime) -> str:
    seconds = int((moment - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "due now"
    if seconds < 120:
        return f"in {seconds}s"
    return f"in {seconds // 60}m"


def show(value: object) -> str:
    return "-" if value is None else str(value)


def performance(row: AgentRow) -> str:
    if row.metrics_available is False:
        return "no metrics"
    if row.violations is None:
        return "-"
    return "bad" if row.violations else "ok"


def counts_line[K: str](counts: Mapping[K, int]) -> str:
    return "  ".join(f"{name} {n}" for name, n in counts.items() if n) or "none"


def condition_text(condition: AgentCondition) -> Text:
    return Text(condition, style=CONDITION_STYLES[condition])


def outcome_text(outcome: CanaryOutcome) -> Text:
    return Text(outcome, style=OUTCOME_STYLES[outcome])


def step_state(step: StepView) -> Text:
    """One word, plus the tries when there was more than one. attempts == 0 means the stage never
    ran: skipped by its guard, or the guard failed. The error is listed under the table."""
    if step.attempts == 0:
        return Text("skipped", style="dim") if step.ok else Text("guard failed", style="red")
    tries = f" ({step.attempts} tries)" if step.attempts > 1 else ""
    return Text("ok" + tries, style="green") if step.ok else Text("failed" + tries, style="red")


def took(start: datetime, end: datetime | None) -> str:
    """How long an operation took, or has been running so far."""
    seconds = int(((end or datetime.now(UTC)) - start).total_seconds())
    if seconds < 120:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def operation_result(op: OperationView) -> Text:
    if op.finished_at is None:
        return Text("running", style="cyan")
    return Text("ok", style="green") if op.failure is None else Text("FAILED", style="bold red")


def operation_errors(op: OperationView) -> list[tuple[str, str]]:
    """(step, error) for each failed step, or ("-", the operation's failure) when no step
    carries it."""
    errors = [(s.name, s.error) for s in op.steps if not s.ok and s.error]
    if op.failure is not None and not errors:
        errors.append(("-", op.failure))
    return errors


def heading(title: str) -> Text:
    return Text(f"\n{title}", style="bold")


def new_table(*headers: str) -> Table:
    """A line under the headers and no borders, so piped output stays plain. On a narrow
    terminal a cell wraps onto the next line instead of being cut short, so an IP or a
    condition is never shown as `10.0.12…`."""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for header in headers:
        table.add_column(header.upper(), overflow="fold")
    return table


# --- renderers: one per response ---

def render_status(r: StatusResponse) -> RenderableType:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("active", r.active_version)
    if r.canary is not None:
        grid.add_row("verifying", f"{r.canary.version}  (canary of {r.canary.agents}, "
                                  f"judged {until(r.canary.deadline)})")
        grid.add_row("", counts_line(r.canary.counts))
    grid.add_row("agents", str(r.agents))
    grid.add_row("", counts_line(r.conditions))
    grid.add_row("incidents", f"{r.open_incidents} open")
    return grid


def render_agents(r: AgentListResponse) -> RenderableType:
    if not r.agents:
        return "no agents match"
    table = new_table("ip", "condition", "desired", "running", "process", "healthy", "perf", "seen", "op")
    for a in r.agents:
        table.add_row(a.ip, condition_text(a.condition),
                      a.desired_version + ("" if a.desired_running else " (stopped)"),
                      show(a.running_version), show(a.process_status), show(a.healthy), performance(a),
                      ago(a.last_heartbeat_at), show(a.current_operation))
    return table


def render_operations(ops: Sequence[OperationView]) -> RenderableType:
    """A table of short columns, one row per step, in the order given (callers pass oldest
    first): the operation's own columns are on its first step's row, and a blank line separates
    operations. Errors get their own table below it, in the same order, where only the message
    wraps, so it stays readable on a narrow terminal."""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for header in ("op", "gen", "started", "took", "result", "step", "state"):
        table.add_column(header.upper(), no_wrap=True)
    errors = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for header in ("op", "gen", "step"):
        errors.add_column(header.upper(), no_wrap=True)
    errors.add_column("ERROR", overflow="fold", style="red")      # only the message wraps
    for op in ops:
        steps = [(s.name, step_state(s)) for s in op.steps] or [("-", Text("no steps", style="dim"))]
        for i, (name, state) in enumerate(steps):
            if i == 0:
                table.add_row(Text(op.type, style="bold"), str(op.generation), ago(op.started_at),
                              took(op.started_at, op.finished_at), operation_result(op), name, state)
            else:
                table.add_row("", "", "", "", "", name, state)
        table.add_section()
        for step, error in operation_errors(op):
            errors.add_row(op.type, str(op.generation), step, error)
    if not errors.row_count:
        return table
    return Group(table, Text("errors", style="bold"), errors)


def render_incidents_table(incidents: Sequence[IncidentRow]) -> Table:
    table = new_table("id", "agent", "kind", "opened", "closed", "sent")
    for i in incidents:
        table.add_row(str(i.id), i.agent_ip, i.kind, ago(i.opened_at), ago(i.closed_at),
                      ("opening" if i.notified else "-") + (" + recovery" if i.recovery_notified else ""))
    return table


def render_agent(r: AgentDetailResponse) -> RenderableType:
    """Least important at the top, most important at the bottom, next to the prompt: history,
    operations (oldest first), the current operation, incidents, the state, and last the agent's
    IP and condition."""
    a = r.agent
    parts: list[RenderableType] = [heading("desired-state history")]
    if not r.history:
        parts.append("none recorded")
    else:
        history = new_table("gen", "version", "running", "restart", "force", "changed")
        for h in reversed(r.history):                    # the daemon sends newest first
            history.add_row(str(h.generation), h.version, str(h.desired_running),
                            str(h.restart_generation), str(h.force_update_generation), ago(h.changed_at))
        parts.append(history)

    parts.append(heading("recent operations"))
    if r.recent_operations:
        parts.append(render_operations(list(reversed(r.recent_operations))))
    else:
        parts.append("none recorded")

    parts.append(heading("current operation"))
    if r.current_operation is not None:
        parts.append(render_operations([r.current_operation]))
    else:
        parts.append("none")

    if r.open_incidents:
        parts.append(heading("open incidents"))
        parts.append(render_incidents_table(r.open_incidents))

    parts.append(Text(""))
    parts.append(Text(f"agent {a.agent_status}, healthy {show(a.healthy)}, performance {performance(a)}, "
                      f"last heartbeat {ago(a.last_heartbeat_at)}, registered {ago(r.registered_at)}"))
    for violation in a.violations or []:
        parts.append(Text(f"violation: {violation}", style="red"))

    compare = new_table("", "desired", "observed")
    compare.add_row("version", a.desired_version, show(a.running_version))
    # Observed values first, so each reads straight across from the desired one; the kind in brackets.
    running = "-" if a.process_status is None else \
        f"{a.process_status == ProcessStatus.RUNNING} ({a.process_status})"
    compare.add_row("running", str(a.desired_running), running)
    acknowledged = f"{r.acknowledged_generation} (acknowledged)"
    if r.failed_generation is not None:
        acknowledged += f", {r.failed_generation} (failed)"
    compare.add_row("generation", str(r.generation), acknowledged)
    compare.add_row("restarts", str(r.restart_generation), f"{r.handled_restart_generation} (handled)")
    compare.add_row("force updates", str(r.force_update_generation),
                    f"{r.handled_force_update_generation} (handled)")
    parts.append(compare)

    parts.append(Text.assemble((a.ip, "bold"), "  ", condition_text(a.condition)))
    return Group(*parts)


def render_versions(r: VersionListResponse) -> RenderableType:
    table = new_table("version", "status", "agents", "created", "verified", "note")
    for v in reversed(r.versions):                       # the daemon sends newest first
        note = v.unstable_reason or ("activated by rollback" if v.activated_by_rollback else "")
        table.add_row(v.version, v.status, str(v.agents), ago(v.created_at), ago(v.verified_at), note)
    return table


def render_canary(r: CanaryResponse) -> RenderableType:
    c = r.canary
    if c is None:
        return "no canary running"
    outcomes = new_table("ip", "outcome")
    for a in c.agents:
        outcomes.add_row(a.ip, outcome_text(a.outcome))

    crashes = c.counts[CanaryOutcome.FAULTY_UPDATE] + c.counts[CanaryOutcome.CRASHED]
    not_converged = c.counts[CanaryOutcome.NOT_CONVERGED]
    converged = sum(c.counts[o] for o in (CanaryOutcome.NO_VERDICT, CanaryOutcome.PERFORMANCE,
                                          CanaryOutcome.PASSED))

    def limited(count: int, limit: int, over: bool = False) -> Text:
        """The count in red when it is over its limit (the canary would be rejected for it)."""
        return Text(f"{count} (max {limit})", style="bold red" if over or count > limit else "")

    counts = Table.grid(padding=(0, 2))
    counts.add_row("crashes", limited(crashes, c.limits.max_crashes))
    counts.add_row("performance", limited(c.counts[CanaryOutcome.PERFORMANCE], c.limits.max_violations))
    # No agent converged rejects the canary whatever the limits, so that shows red too.
    counts.add_row("not converged", limited(not_converged, c.limits.max_not_converged,
                                            over=converged == 0 and not_converged > 0))
    counts.add_row("no verdict", str(c.counts[CanaryOutcome.NO_VERDICT]))
    counts.add_row("passed", str(c.counts[CanaryOutcome.PASSED]))

    title = Text(f"Canary for {c.version} started {elapsed(c.created_at)} ago", style="bold")
    return Group(outcomes, counts, Text(""), title)      # the title last, next to the prompt


def elapsed(moment: datetime) -> str:
    """"1 min 20 seconds", or "20 seconds" under a minute."""
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds} seconds" if minutes else f"{seconds} seconds"


def render_incidents(r: IncidentListResponse) -> RenderableType:
    return Group(                                        # the open ones last, next to the prompt
        Text("recently closed", style="bold"),
        render_incidents_table(list(reversed(r.recently_closed))) if r.recently_closed else "none",
        heading("open"),
        render_incidents_table(r.open) if r.open else "none",
    )


def render_change(r: DesiredStateChangeResponse) -> RenderableType:
    d = r.desired
    return (f"{r.ip}: generation {d.generation}  version {d.version}  running {d.desired_running}  "
            f"restart {d.restart_generation}  force {d.force_update_generation}\n"
            f"follow it with: cm agent {r.ip}")


def render_decommission(r: DecommissionResponse) -> RenderableType:
    return f"{r.ip}: decommissioned; its heartbeats are refused from now on"


def render_dispatch(r: DispatchResponse) -> RenderableType:
    return (f"{r.version}: verifying on {len(r.canary_agents)} agents "
            f"({', '.join(r.canary_agents)}), judged {until(r.deadline)}")


def render_rollback(r: RollbackResponse) -> RenderableType:
    return f"rolled back {r.from_version} -> {r.to_version} on {r.agents} agents"


# --- the app ---

app = typer.Typer(
    name="cm",
    help="Cluster manager CLI. Every command is one request to the daemon, except `cm daemon`.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,    # `cm daemon` tracebacks must not print config values
)


@dataclass(frozen=True, slots=True)
class Options:
    daemon_url: str
    as_json: bool


@app.callback()
def global_options(
    ctx: typer.Context,
    daemon_url: Annotated[str, typer.Option("--daemon-url", envvar="CM_DAEMON_URL",
                                            help="The daemon to talk to.")] = default_daemon_url(),
    as_json: Annotated[bool, typer.Option("--json", help="Print the daemon's response as JSON.")] = False,
) -> None:
    ctx.obj = Options(daemon_url=daemon_url, as_json=as_json)


def send[R: BaseModel](ctx: typer.Context, request: Callable[[DaemonClient], R],
                       render: Callable[[R], RenderableType]) -> None:
    """Sends one request and prints the response, rendered or as JSON."""
    options: Options = ctx.obj
    client = DaemonClient(options.daemon_url)
    try:
        response = request(client)
    except DaemonError as e:
        error_console.print(f"cm: {e.detail}", style="red")
        raise typer.Exit(code=1)
    except httpx.HTTPError as e:
        error_console.print(f"cm: can't reach the daemon at {options.daemon_url}: {e}", style="red")
        raise typer.Exit(code=2)
    finally:
        client.close()
    if options.as_json:
        typer.echo(response.model_dump_json(indent=2))
    else:
        console.print(render(response))


# --- the daemon ---

@app.command()
def daemon() -> None:
    """Run the daemon in the foreground (what systemd runs)."""
    # Imported here: the daemon's config reads .env at import, and a machine that only runs the
    # client has none.
    from cluster_manager.daemon.__main__ import main as run_daemon
    run_daemon()


# --- reads ---

@app.command()
def status(ctx: typer.Context) -> None:
    """Is the fleet OK?"""
    send(ctx, DaemonClient.status, render_status)


@app.command()
def agents(
    ctx: typer.Context,
    problems: Annotated[bool, typer.Option("--problems",
                                           help="Only agents that aren't converged or stopped.")] = False,
    condition: Annotated[AgentCondition | None, typer.Option(help="Only agents in this condition.")] = None,
    version: Annotated[str | None, typer.Option(help="Only agents whose desired version is this.")] = None,
) -> None:
    """One line per live agent."""
    send(ctx, lambda c: c.agents(condition=condition, version=version, problems=problems), render_agents)


@app.command()
def agent(ctx: typer.Context, ip: str) -> None:
    """Everything about one agent: why is it like that?"""
    send(ctx, lambda c: c.agent(ip), render_agent)


@app.command()
def versions(ctx: typer.Context) -> None:
    """Every version and its status."""
    send(ctx, DaemonClient.versions, render_versions)


@app.command()
def canary(ctx: typer.Context) -> None:
    """The running canary, as of now."""
    send(ctx, DaemonClient.canary, render_canary)


@app.command()
def incidents(ctx: typer.Context) -> None:
    """Open and recently closed incidents."""
    send(ctx, DaemonClient.incidents, render_incidents)


# --- per-agent actions ---

@app.command()
def restart(ctx: typer.Context, ip: str) -> None:
    """Restart the agent's worker (bumps restart_generation)."""
    send(ctx, lambda c: c.restart(ip), render_change)


@app.command("force-update")
def force_update(ctx: typer.Context, ip: str) -> None:
    """Re-download the version and start fresh, for a crashed or stopped worker (bumps force_update_generation)."""
    send(ctx, lambda c: c.force_update(ip), render_change)


@app.command()
def stop(ctx: typer.Context, ip: str) -> None:
    """Keep the agent's worker stopped (desired_running=false)."""
    send(ctx, lambda c: c.stop(ip), render_change)


@app.command()
def start(ctx: typer.Context, ip: str) -> None:
    """Run the agent's worker again (desired_running=true)."""
    send(ctx, lambda c: c.start(ip), render_change)


@app.command()
def decommission(ctx: typer.Context, ip: str) -> None:
    """Refuse this agent's heartbeats from now on."""
    send(ctx, lambda c: c.decommission(ip), render_decommission)


# --- versions ---

@app.command()
def dispatch(ctx: typer.Context, version: str, upstream_url: str) -> None:
    """Put a new version on the file server and canary it."""
    send(ctx, lambda c: c.dispatch(version, upstream_url), render_dispatch)


@app.command()
def rollback(ctx: typer.Context) -> None:
    """Roll the fleet back to the latest STABLE version."""
    send(ctx, DaemonClient.rollback, render_rollback)
