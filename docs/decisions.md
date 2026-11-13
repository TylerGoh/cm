# Decisions

Key decisions behind the current design. Each entry states what is decided, why, and the cost where there is one. Only current decisions are recorded: when a decision changes, its entry is rewritten or removed. Entries settled from 2026-11-04 on carry their date.

## Principles

**Reads go anywhere, writes have one path.**
Every state change goes through `decide` and an operation. `_set_memory` is the only writer of `AgentMemory`; `transition_to` is the only mover of `ProcessManager`'s state.
Why: every change is traceable to one decision and one operation.

**Derive labels, store facts.**
Values computable from what's observable now are derived, never stored (`ProcessStatus.CRASHED`, `AgentStatus`, "updating"). Things that happened are persisted (counters, `failed_generation`, `crash_restarted`).
Why: stored labels go stale and can't be left stuck by a crash; past facts can't be recomputed.

**One snapshot per tick.**
`Observed` is built once per tick (including `healthy`, read once) and both the report and `decide` use it.
Why: the report always describes exactly what the decision was based on.

**The managers don't know each other.**
Cross-manager data flows through pipeline context keys; the supervisor is the only component holding both.
Why: independently testable, one-directional dependencies.

**The heartbeat is the only status channel.**
The agent serves no routes and the cluster manager never calls an agent. Silence means unreachable.
Why: one source of truth, no inbound access to agents, no probe state.

**Explicit over clever; names show where they come from.**
Recipes are written out step by step. Names carry their origin (`MetricsReading`, not `Reading`).
Cost: more typing.

**Persisted models have no field defaults.**
First-boot values are built explicitly.
Why: a missing field in a stored file fails loudly.

## Agent policy

**The agent only acts on a desired state received this tick.** A failed heartbeat means do nothing.
Why: never acts on a stale desired state.

**One operation at a time, restarted from scratch.** Every step is idempotent; nothing resumes.
Why: no partial-progress state to persist or reason about.

**The agent never rolls back.** The cluster manager rolls back by changing the desired state.
Why: rollback is policy, owned by the component that sees the whole cluster.

**`decide` is pure.** It gets values only, no IO, and never reads `healthy` or `violations`.
Why: policy stays testable. `decide` says what, `operations` how, the supervisor when.

**`generation` identifies one change to one agent's desired state.**
The agent works through it over as many ticks and operations as needed; only `failed_generation == generation` stops it. Acknowledged means received, not finished.

**Generation counters start at 0 on both sides.** No baselining; the cluster manager resets them on re-provisioning.

**Crash budget: one automatic CRASH_RESTART, then CRASHED_HOLD.**
Only UPDATE and FORCE_UPDATE reset `crash_restarted`. A force bump is the way out of CRASHED_HOLD, and of a STOPPED worker whose start failed (2026-11-05). An agent restart ending CRASHED_HOLD for a dead worker is accepted.
Why: the budget is a convenience, not a safety mechanism, so it stays simple.

**`plan()` runs before the crash budget is spent; a `plan()` error is a bug, not a failed operation.**
It ends the tick loop and systemd restarts the agent.
Why: `plan()` does no IO, so an error says nothing about the desired state.
Cost: a deterministic bug restart-loops until systemd's start limit.

**"Updating" is derived from the operation type** (`UPDATING_OPS`), not a flag.
Cost: it covers the whole operation, not one phase.

**The last operation isn't persisted.** It's in memory for the report and logged as one JSON journal line; the cluster manager keeps history. Cancelled operations aren't logged.

## Process management

**`ProcessManager` owns every `ProcessStatus` rule.**
`derive_status(responsive)`: a unit not alive while STARTING/RUNNING → CRASHED; RUNNING but unresponsive → CRASHED (hung). CRASHED is never stored.
Why: one place decides the status; a hang is the same derivation as a dead worker.
Cost: a long stall under load could read as a hang, so `max_failures × interval` must be generous.

**`transition_to` moves the agent's in-memory process state.**
Allowed: STOPPED → STARTING, STARTING → RUNNING/STOPPING, RUNNING → STOPPING, STOPPING → STOPPED. Same-state is a no-op.

**`reattach` resets a failed unit and counts it STOPPED.** The agent only reports a crash it was alive to see.
Cost: no crash evidence for a worker that died while the agent was down.

**Every stop is a SIGKILL.** No SIGTERM rung, no `force_kill`.
Why: the worker is slow to die and a hung one ignores SIGTERM anyway.
Cost: the worker never shuts down gracefully.

**The worker is a systemd unit the agent drives with `systemctl` (2026-11-13).**
`WORKER_SYSTEMCTL_NAME` names the unit, installed with the box with `Restart=no`, `KillSignal=SIGKILL` and the default `KillMode=control-group`. Alive is the unit's `ActiveState`; the agent's STOPPED/STARTING/RUNNING/STOPPING is held in memory and rebuilt by `reattach`, so there is no `process.json`. `start` and `stop` are `--no-block`; the agent waits in its own loops. `start` refuses unless STOPPED. A crash is any death the agent didn't ask for, exit 0 included. A unit running while the agent has it STOPPED is adopted by a REATTACH. The old spawned workers were stopped by hand on patch day.
Why: the worker was already installed as a unit. systemd tracks every process in the unit's cgroup, so no pid reuse, no zombies, and a worker's children are gone before it counts as stopped; its stdout and stderr go to the journal. `systemctl` needs no new dependency, and is what the owner runs by hand.
Cost: each status check runs a `systemctl` process (a few ms, once per tick and every 0.5s in a wait). A process SIGKILL can't take (D state) for two `TimeoutStopSec` is reported `failed` while still there. The worker's environment comes from its unit, not the agent.

**Starting ends with `wait_for_log`; a timeout kills the process and ends on the crash path.**
`start` returns the program log's size from before the start, and `wait_for_log` reads forward from there. `start` and `wait_for_log` have `retries=0`. No finally steps.
Why: a start that never gets ready is a failed worker, which the crash path already handles.

**REATTACH waits with `wait_for_version`, not the log, for any live worker.**
Why: a new agent doesn't know whether the current run logged its ready line, or where its lines begin.

**Health is the version endpoint.** `HealthMonitor` is active while RUNNING or CRASHED and no operation runs. The worker only stops answering during agent-driven operations.
Cost: a hang verdict is lost after an operation or agent restart.

**The worker survives an agent restart:** it is its own unit, outside the agent's cgroup.

**Two worker logs:** its stdout and stderr in the unit's journal, and the worker's own program log, which `wait_for_log` reads.

## Recipes

| Op | Steps |
|---|---|
| REATTACH | `reattach, wait_for_version (when), stop (when)` |
| START | `start, wait_for_log` |
| STOP | `stop` |
| RESTART | `stop, start, wait_for_log` |
| CRASH_RESTART | `stop, start, wait_for_log` |
| UPDATE | `download, clear_hypertree*, fetch_hypertree*, reload, verify (retries=1), upload_hypertree*` |
| FORCE_UPDATE | `download, clear_hypertree*, fetch_hypertree*` |

`*` = non-critical. No recipe has finally steps.

**Only UPDATE verifies the version.**
Why: the others bring a worker up, they don't choose what it runs. Verifying `desired.version` in a restart would block the UPDATE that fixes it.

**Every step that can hang has its own timeout; the cluster manager has no operation deadlines.**
Why: the agent owns mechanics and can actually end an operation.

**Binary installation is outside the agent.** A box always has a default artifact; there is no `commit` and no `cleanup`.

**Artifacts are bundles: one unpacked folder per version, linked into `etc_dir` per file.**
Unpacked into `<version>.part` then renamed; links are swapped atomically. A real file at a link target is refused, never moved or deleted.
Why: switching versions only ever changes links; the default install is never harmed.

**Hypertrees are best effort.** Cleared before the load, fetched if present, and the one folder left after a successful load is zipped and uploaded.
Why: a hypertree only speeds loads up, so it must never fail an update.

**FORCE_UPDATE clears and fetches the hypertree; CRASH_RESTART never uploads one (2026-11-05).**
Why: the CRASH_RESTART after a FORCE_UPDATE loads the new bundle, so it should start from the stored hypertree. An upload only speeds up later loads, which isn't worth risking a recovery for.

**Reload phases are blocking GETs; success means done (2026-11-05).**
Confirmed by the owner: each phase returns only when it has finished, so the agent never waits after one.

**Reload is two worker calls in one step** (`reload_phase1`, `reload_phase2`).
Why: a retry must repeat both.

**Conditional steps use guards.** A skipped step is `ok=true, attempts=0`, which the cluster manager shows as skipped.

## Pipeline engine

- Checks run before any side effect: stage signatures, stage fit, initial context.
- Finally steps run on success, failure and cancellation, and can't depend on produced keys.
- `retries` counts retries after the first try; negative is rejected.
- `failure` is set only inside `run`.

## Monitors and metrics

**Monitors read a `ConditionFeed` and run as explicit tasks.** A monitor crash is logged and can't take down the supervisor.

**`violations` is three-valued and free text:** `None` no verdict, `[]` ok, list = problems. Nothing parses the messages.

**Performance is one failed-request ratio over a 5-minute window** from cumulative counters (`failed / (out + failed) > 0.30`).
Cost: no verdict for the first 5 minutes after an update.

**A broken metrics source is reported as `metrics_available=False`;** the cluster manager alerts on it.

**`WorkerClient` queries return `None` on failure; commands raise; `metrics()` is all or nothing.**

**Every worker request is the base URL with a command appended as-is** (2026-11-05).
The worker's base URL (`config.worker_url`) ends where the command name goes, typically in a query such as `...?command=`, so the contract constants are bare command names (`show_version`, `counters`, `reload_phase1`, `reload_phase2`) with no leading slash, and `WorkerClient` builds each URL as `base_url + command`. It doesn't give the base URL to `httpx`. The metrics generator's YAML says `*_command` and `command:`, and refuses a leading slash.
Why: `httpx`'s `base_url` always adds a `/` to the base, so `...?command=` plus `show_version` became `...?command=/show_version`.
Cost: nothing checks the base URL's shape; one that ends in the wrong place sends every command to the wrong URL, which shows up as health and metrics failures.
A missing metric name is logged once, and again only after a complete reading (2026-11-05).

**The worker contract is generated from YAML; the judgement is hand-written (2026-11-05).**
A YAML description of the worker (paths, endpoints, each metric's name, field and counter/gauge type) becomes the block between the markers in `worker_client.py`; since 2026-11-06 that is `config.yaml`'s `worker:` section, applied by `prepare.py`. An endpoint may mix counters and gauges. The performance judgement in `performance.py` (renamed from `metrics.py`) is coded by the owner, from the sample in `docs/worker-contract.md`.
Why: the contract is pure description, so a generator keeps it consistent; the judgement is policy, which the owner wants to write himself.

## Elastic

**Best effort, never raises, no queue, no logs.** Logs go to the journal only.

**The worker's metrics are exported on their own interval, under their field names (2026-11-05).**
A metrics exporter loop sends one document per `METRICS_EXPORT_INTERVAL` while the worker is RUNNING: the reading's fields, plus `at`, `running_version`, `updating` and the host fields. Counters go cumulative. A metric has two names only: the intake name (the worker's JSON key, the `validation_alias`) and the field name, which is also its Elastic name.
Why: the owner wants the raw metrics in Elastic on a fixed cadence; the worker's dotted names would become nested objects in Elastic, so they stay on the intake side.
**Two indexes, metrics and host;** the client adds `hostname` and `ipv4` to every document (`host` became `hostname` and `location` was dropped on 2026-11-06; a location is now an inventory `env:` field if wanted). TLS uses the CA in `elastic.cert`; a missing cert fails startup.

**Elastic requests authenticate with an API key** (2026-11-05).
`config.elastic_api_key` (a `SecretStr`) is sent on every request as `Authorization: ApiKey <key>`, the encoded key as Elastic issues it. TLS verification against `elastic.cert` stays.
Why: the Elastic cluster requires an API key. `SecretStr` keeps the key out of reprs and logs; it is unwrapped only where the client is built.
Cost: a wrong or revoked key only shows as logged write failures (401), since Elastic writes never raise; documents are lost until the key is fixed.

**The official async Elasticsearch client replaces raw httpx (2026-11-11).**
`ElasticClient` wraps `AsyncElasticsearch` with `api_key` and `ca_certs`, on its httpx node, with no retries. Each failure is logged as what went wrong (Elastic's error type and reason, TLS, timeout, refused connection) when it starts and when it changes, per index.
Why: the owner wanted the standard client and better error handling; the raw client's errors didn't say why a write failed. Async keeps the event loop free; the httpx node avoids aiohttp.
Cost: five more packages on the internal index (`elasticsearch`, `elastic-transport`, `urllib3`, `python-dateutil`, `tzdata`), and the client's major version must match the cluster's.

## Agent app

- FastAPI under uvicorn, passing the app object (rules out multiple workers and reload). Wiring is a lifespan.
- uvicorn owns signals; shutdown cancels the tick loop. A tick-loop crash SIGTERMs the process.
- Fixed 10s tick, 20s shutdown grace, `TimeoutStopSec=40`.
- `_set_memory` saves before updating memory and takes typed changes (`AgentMemoryChanges`).
- Settings come only from `.env`: no prefix, environment ignored, unknown keys rejected. Everything binds `0.0.0.0`.
- Host identity (IPv4, hostname) comes from config, written by Foreman.

## Wire

- The reply is the bare, flat `DesiredState`. No envelope, no `protocol_version`, no maintenance.
- One contract; a wire change is deployed to both sides together. Both `wire.py` copies are identical.
- An agent is its IPv4 address (`IPv4Address` on the wire, strings in the DB). No hostname on the wire.
- The first heartbeat from an unknown IP registers the agent, seeded from the report. Decommissioned IPs are refused; who may join is an external whitelist.
- Every timestamp is `AwareDatetime`.
- `OperationView` is used exactly on both sides and carries `generation`.
- `running_version` is stored as reported, `null` included.
- Agents receive complete file-server URLs; they never build them.

## Cluster manager

**A daemon driven by a CLI over HTTP**, sharing a schemas module. Services own transactions; repositories never commit.

**Versions: VERIFYING, ACTIVE, STABLE, UNSTABLE.** At most one VERIFYING, exactly one ACTIVE, enforced in the database. UNSTABLE is never deployed again. A version's id is the worker's version string (upstream's job id).

**Bootstrap:** on an empty table, `config.bootstrap_version` is fetched and seeded as a verified ACTIVE. Afterwards the setting is ignored.

**Dispatch:** the file server fetches the artifact first (and returns its sha256); only then does the canary start. Existing versions and concurrent dispatches are refused.
Why: agents depend only on infrastructure we own, and no canary starts on an unavailable artifact.

**Canary:** a random fixed-size sample of live, running agents; membership is derived from `desired_state.version`. Judged once at a deadline: each agent gets one outcome, counted against per-bucket limits (crashes, performance, not converged). Passing promotes automatically.
Cost: until metrics flow, a canary can only fail on crashes, faulty updates or non-convergence.

**A canary never passes without converged agents (2026-11-05).**
A canary where no agent runs the new version at the deadline is rejected whatever the limits, including one whose agents were all decommissioned. The example settings put `CANARY_MAX_NOT_CONVERGED` at 0, so any agent not converged rejects it.
Why: passing would promote a version on no evidence.

**At most one rollback in a row.** An ACTIVE that came from a rollback is never rolled back. `unstable_reason` records why.

**A promoted version is judged for as long as it is ACTIVE, and rolled back automatically (2026-11-05).**
The fleet watch judges the agents on the ACTIVE version every tick by the canary's rules, with higher limits (`FLEET_MAX_*`). A breach that lasts `FLEET_BREACH_SECONDS` rolls back to the latest STABLE. Agents still settling (not yet acknowledged, or mid-operation), silent agents and agents stopped on purpose don't count.
Why: the owner wants the same health rules after promotion, needing more failing agents to act, and no human in the loop. Settling agents are left out, or every promotion would fail at once; a breach must last, or a crash caught between the worker dying and the agent restarting it could roll back a good version.
Cost: a version that fails only slowly, or on fewer agents than the limits, stays ACTIVE.

**No verdict at the canary deadline is fine (2026-11-05).** An agent whose metrics are unavailable is NO_VERDICT, which counts in no bucket.

**Non-responding after 3 missed ticks.** Incidents (`non_responding`, `metrics_unavailable`), at most one open per agent and kind. A sender posts openings and recoveries to Mattermost after commit, marking each sent only on success.

**An incident records its source and an IP, with no foreign keys (2026-11-13).**
`source` is `AGENT` or `CLUSTER_MANAGER`; `ip` is the agent's, or the daemon's own `IPV4` from its `.env`; `version` names the version a cluster manager incident is about. At most one open per source, IP, version and kind.
Why: the daemon raises incidents of its own (a re-fetched artifact whose hash doesn't match), and an incident is information that shouldn't depend on other tables. The daemon's IP is set by hand because detecting it is unreliable, and under Kubernetes it would be a changing pod IP.
Cost: an incident no longer links to its agent row, and `IPV4` is one more key to fill in `config.yaml`.

**Database:** PostgreSQL (the Bitnami image under podman, 2026-11-05: the RHEL 8 box's SQLite is too old), `READ COMMITTED`, all access through SQLAlchemy, no literal SQL. Enums stored by name. Timestamps are `UtcDateTime` from the daemon's clock. Indexes and constraints are expressions over model attributes. Desired-state changes are one atomic `UPDATE ... RETURNING`; history keyed `(ip, generation)`. Operations recorded once per `(agent_ip, op_id)`. Schema via `create_all`, no migrations while data can be wiped.

**The 2026-11-13 schema change wipes the database: `tools/migrate_2026_11.py` (2026-11-13).**
It drops every table and enum type and creates the current schema empty, in one transaction, only with `--yes` and only on a database not already on the new schema. Agents register again on their next heartbeat, and the daemon's next start bootstraps `BOOTSTRAP_VERSION` again. It ships in the patch (under `tools/` in the install folder) and is copied to the daemon's box and run there with the daemon's `.venv` Python, the daemon stopped.
Why: some stored versions came from corrupt upstreams, and the owner wants a clean slate over migrating them. The script is maintained only by Claude, so it may use handwritten SQL.
Cost: incident history, operations and the record of decommissioned agents are lost; a decommissioned box still running registers again. The file server's copy of the bootstrap version must be deleted by hand first, or the bootstrap gets the stored copy back.

**The CLI is Typer, rendered with Rich (2026-11-04).**
Each command is a typed function whose docstring is its help, enum options list their choices and are checked before a request is sent, and lists render as tables with only condition, canary outcome and step state coloured. Daemon text is printed with markup off; piped output keeps one row per line; tracebacks hide local variables.
Why: cleaner command definitions than argparse plus a hand-written table helper, and a readable `--help` without extra work.
Cost: two more dependencies (typer, rich) in the cluster manager.

**Inventory fields are agent settings only; the states are always sent (2026-11-12).**
`config.yaml`'s inventory fields are `.env` settings, generated as a plain dict that reads `config` directly (`"rack": config.rack`), and go into each interface record. The desired and observed states are fixed code in the state document, no longer configured. `prepare.py` does all the checking; the agent trusts what it generated.
Why: the configurable mix of feed values and settings (`FeedField`, `SettingField`, the agent's own checks) was abstraction nobody used; the owner only adds settings.
Cost: `inventory.py` imports `config`, so it has no unit tests; code that needs `config` is covered by devenv instead.

**Generated blocks are marked `# BEGIN generated: <name>` / `# END generated: <name>`, with no comments inside (2026-11-12).** `prepare.py` and the patch check also read the older `# --- ... ---` form that v4 installs have.

**The judgement is a user block, `judge(oldest, newest)`, shipped commented out (2026-11-13).**
`performance.py` has `# BEGIN user: judge` / `# END user: judge`, holding only a commented-out `judge(oldest, newest) -> list[str] | None` with `pass`. The owner writes `judge`, its helpers and its limits there on the box. `MAX_FAILED_RATIO` and the monitor's `max_failed_ratio` are gone.
Why: the judgement is the owner's code, and two readings are all a judgement needs; its limits belong with it. An agent without it should fail loudly, not run with no judgement.
Cost: until the block is filled in, every full window logs a `NameError` and gives no verdict, devenv included. Patches don't yet carry the box's block into a new `performance.py`.

**The agent creates its folders at startup (2026-11-13).**
`_ensure_folders` in `app.py` makes, before anything else: the store's folder, the program log's folder, `DOWNLOAD_DIR`, `BUNDLE_DIR`, `ETC_DIR` and `HYPERTREE_DIR`. The `mkdir` calls at first use stay, for a folder deleted while the agent runs.
Why: a missing folder failed only at its first write, sometimes long after startup, and installing needed a hand-made `/var/lib/agent`.
Cost: a mistyped path in `.env` is created instead of rejected.

## File server

Single-file FastAPI app, one directory per job. The fetch is idempotent, hashes while streaming, rejects anything that isn't a valid zip, and writes `job.json` last. One hypertree URL per job (GET/PUT, 409 once present). No auth, one process.

**The file server keeps every job (2026-11-05).**
Why: storage is cheap, and every stored job stays available for a rollback or a re-dispatch.
Cost: disk grows with every job.

**A job is stored only while its artifact is there; a missing one is fetched again (2026-11-13).**
The fetch checks for `job.json` and an artifact of its size, not `job.json` alone, and fetches again from the request's upstream URL otherwise. The daemon can pass the sha256 it recorded: an artifact with another hash is refused with 409 and not stored.
Why: a version deleted by hand on the file server stayed missing, and with only the artifact gone, a new fetch did nothing. Refusing a changed artifact keeps the file server from serving one that agents would reject, and lets a later fetch succeed once upstream serves the right one again.
Cost: a missing hypertree isn't restored; the next agent to load the version makes and uploads a new one.

**The daemon fetches every version that isn't UNSTABLE again, every minute (2026-11-13).**
The file watch calls the idempotent fetch with the recorded `upstream_url` and sha256. A changed artifact opens an `ARTIFACT_MISMATCH` incident from the cluster manager; a fetch that succeeds closes it.
Why: any version that isn't UNSTABLE can be pointed at again (ACTIVE, the canary, a rollback target), and nothing else noticed a version deleted on the file server.
Cost: one request per such version per minute, and an incident still open when its version becomes UNSTABLE stays open.

## Packaging

**Three separate projects, `agent`, `cluster-manager` and `file-server` (2026-11-04).**
Each has its own `pyproject.toml`, `.gitignore`, `.env.example`, README and `deploy/` unit, and its own virtualenv. No project imports another. The agent's project and distribution are named `agent`, its unit `agent.service`, its folder `/opt/agent`.
Why: each goes to a different machine, so each must install on its own.
Cost: nothing checks the two `wire.py` copies match; `agent` and `/opt/agent` are generic names on a shared box.

**Installed by hand, from each project's README; no install scripts (2026-11-04).**
The steps: virtualenv in the project folder, `pip install -e .`, `.env` from `.env.example`, run the command in the foreground, then copy the unit from `deploy/`. The owner sets up the virtualenvs and units on every machine himself (2026-11-05), so pip access and the units' user are his to arrange; the units in `deploy/` are a starting point.
Why: the owner installs each machine himself and wants to see every step. The install is editable so each config finds the `.env` next to its own source.

**Every service is one console script, and systemd runs the same command (2026-11-04).**
`agent`, `cm daemon` and `file-server`. Each runs uvicorn with the app object, so each is one process.
Why: a clean way to run each service in a terminal, identical to what the unit runs. `cm daemon` keeps the cluster manager to one command.

**Every service reads `.env` from its own project folder (2026-11-04).**
The daemon's `daemon.env` became `.env`, and the file server moved from environment variables (with a default timeout) to a `.env` read by pydantic-settings with the same rules as the others: no defaults, unknown keys rejected, environment ignored. Its port moved from the unit's command line into `.env`.
Why: one rule for all three, and a foreground run needs nothing exported.

**The code lives in the GitHub repository `TylerGoh/cm`; a release is a commit (2026-11-04).**
The docs live in its `docs/`. A release zip is `git archive` of a commit.
Why: moves, renames and small edits are cheap in git and expensive as whole-document rewrites in the claude.ai project.

**`rename_term.sh` is a fixed checklist of placeholders (2026-11-05; replaced 2026-11-06).**
Only `hypertree`, `etc_dir` and `program_log` could be renamed: the owner filled in each real name at the top of the script and ran it once, on a fresh copy.
Why: renaming any term typed on the command line was too loose; the checklist records exactly which names are placeholders.

**One `config.yaml`, applied by `prepare.py`, drives the initial install and every patch (2026-11-06).**
The owner keeps one private `config.yaml`: the renames (now also `reload_phase`), the worker contract, the inventory fields (agent settings, new settings included) and each service's `.env` values. Every zip carries `prepare.py`, which validates it, renames, writes the generated blocks and writes the `.env` files. Commands are format strings over the agent's settings (`reload_app_phase2{config.etc_dir}`). Unknown keys are skipped with a warning. Releases are tags `v0`, `v1`, ...: `cluster_manager-initial.zip` is a full version, and each later version is a patch zip with per-service `old/`/`new/` copies and an `apply.sh` that refuses a box whose files aren't at the previous version (outside the generated blocks) unless `--force`. Version numbering was reset to `v0` at this change.
Why: the proprietary parts (names, worker contract, what is sent to Elastic, settings) can't be in the repository and were being pasted or renamed by hand on each box; patches that replaced whole files broke on them. One file the owner keeps, applied the same way to a fresh install and to every patch, keeps every box consistent and survives between sessions.

**`check_wire.sh` compares the two `wire.py` copies (2026-11-05).**
Run by hand before a release; a difference fails with the diff.
Why: the copies live in separate projects, so no single install or test sees both.

**A patch uses only the install folder's config.yaml (2026-11-11).**
Patches are `patch_cm-vN.zip`. Its `apply.sh INSTALL_FOLDER` is the only way to apply one: it runs the patch's `prepare.py --install INSTALL_FOLDER`, which reads `INSTALL_FOLDER/config.yaml` and refuses a `config.yaml` inside the patch; the per-service `apply.sh` files run only from it. The by-hand route (copy a config into the patch, apply per box) is gone.
Why: a config copied into a patch could be stale or another install's, and the patch would then rename and generate wrong without any check failing. One source rules that out.

**Step names are the method names (2026-11-05).** `start` and `stop`; nothing depends on the earlier `spawn_process` and `kill_instance`.
