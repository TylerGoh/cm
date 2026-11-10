# devenv: a mock cluster to watch the cluster manager work

Dev-only: never in a release zip (`.gitattributes` export-ignore; `tools/make_patch.py` skips it).

```bash
./devenv/dev                         # opens the TUI (builds devenv/.venv with uv on first use)
./devenv/dev test -m "not scenario"  # quick tests
./devenv/dev test                    # every scenario for real, about 25 minutes
```

## What runs

On launch, the base state: PostgreSQL (from the `pgserver` wheel, no root or Docker), a fake
upstream, the file server and the daemon, which boots with `job_1` ACTIVE and no agents.
Running a scenario resets to that, then starts two real agents (10.99.0.1, 10.99.0.2), each of
which spawns a fake worker (`harness/fake_worker.py`, a real process with a real pid).

| What | Port |
|---|---|
| daemon | 54000 |
| file server | 54080 |
| fake upstream | 54090 |
| agents | 54101, 54102 |
| fake workers | 54201, 54202 |
| PostgreSQL | 54432 |
| Elastic (nothing listens; the agents log one warning) | 54999 |

Each service runs from a fresh copy of its project folder under `devenv/.run/` with its own
`.env`, so your project folders are never written. Settings are in `harness/env.py`: canary
size 1, every canary and fleet limit 0, a 120s canary, a 30s performance window, a 15s fleet
breach. If a project's `.env.example` gains or loses a key, devenv says so instead of starting.

The fake worker answers `show_version`, `counters`, `reload_phase1` and `reload_phase2` with
delays (startup 3-8s, each reload phase 5-15s, queries up to 0.5s; at most 30s). Scenarios
toggle it through `/control`: bad numbers, hang, crash, delays. A version with `bad` in its name
always gives bad numbers.

## The TUI

- Top left: the scenarios, their state, step and what they're waiting for.
- Top right: the selected scenario's transcript: each changing command as `cm ...` with cm's
  full answer, and each check.
- Bottom: read-only `cm` (`status`, `agents [--canary]`, `agent <ip>`, `versions`, `canary`, `incidents`,
  with `--json` if you like), `run <scenario>`, `help` and `exit`.

Enter runs the highlighted scenario, Esc stops it, Tab completes. After a scenario ends the
environment stays up for `cm` until the next run. Logs are in `devenv/.run/logs/`.

Quitting (`exit`, Ctrl-C, or q in the list) stops everything first and shows each step: agents, workers,
daemon, file server, upstream, PostgreSQL, any stray process carrying `DEVENV_RUN`, then
`devenv/.run/`. A second Ctrl-C kills what's left at once. The next launch clears anything a
`kill -9` stranded.

## Scenarios

| Scenario | Shows |
|---|---|
| promote | `job_2` passes its canary and reaches the fleet |
| canary-rejected | `job_2_bad` reports bad numbers: violation, rejected, UNSTABLE, canary agent back on `job_1` |
| fleet-rollback | `job_2` promoted, then both workers turn bad: the fleet watch rolls back to `job_1` |
| crash-restart | a worker exits; its agent does a CRASH_RESTART |
| hang | a worker stops answering; the agent fails its liveness checks and restarts it |
| reattach | an agent stops and starts; it reattaches to the worker it left running |
| manual-rollback | `cm rollback` refused (no STABLE), accepted, then refused (rollbacks never chain) |

Add one in `harness/scenarios.py` with `@scenario(...)`.
