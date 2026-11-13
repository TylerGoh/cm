# Agent supervisor: current architecture

The current, settled design. Reasons for decisions live in `decisions.md`; unresolved questions live in `open-items.md`.

The code is three separate projects, one per service: `agent/`, `cluster-manager/` and `file-server/` (see "Packaging and deployment"). Each section below names its files by their path inside their project. The code is the source of truth for signatures and implementations. This file holds the layout, the rules the code must follow, and the reasons the code can't show.

## Layout and import direction

The agent's project, `agent/`:

```
config.py          the agent's settings, read only from .env next to it (from config import config)
agent/
  __main__.py      logging setup; runs uvicorn with the app object (the `agent` command)
  app.py           FastAPI app: lifespan wiring, tick loop, start record, explicit tasks, shutdown order
  supervisor.py    tick, observe, sync, report, publish conditions, start/drive/finish, ActiveOperation
  decide.py        pure policy
  operations.py    plan(): one explicit recipe per OpType
  pipeline.py      generic engine; Stage.from_callable and Guard.from_callable derive from signatures (imports nothing from the agent)
  process.py       ProcessManager: owns ProcessStatus; spawns, waits for, stops and reattaches to the worker (no pipeline import)
  updates.py       UpdateManager: bundles and links, hypertrees, reload, verify (no pipeline import)
  worker_client.py the only code that sends requests to the worker; the generated worker contract block
  feed.py          Conditions, ConditionFeed
  health.py        HealthMonitor (own loop)
  performance.py   PerformanceMonitor (own loop): failed-request ratio over a sliding window
  inventory.py     InventoryExporter (own loop): host record to Elastic
  metrics_exporter.py  MetricsExporter (own loop): the worker's metrics to Elastic
  elastic.py       ElasticClient: data documents only, two indexes, awaited directly, best effort
  models.py        agent-internal runtime and persisted models, HostInfo
  wire.py          contract with the cluster manager
  cluster_manager_client.py  heartbeat client
  state_store.py   JsonStore[T] with atomic save
tests/
  test_models.py         AgentMemoryChanges mirrors AgentMemory's fields
  test_performance.py    judge, full window, counter reset, gate reset, metrics_available, settings
  test_worker_client.py  metrics all-or-nothing, missing names logged once, queries return None, health on the
                         version endpoint, reload phases
  test_metrics_exporter.py  documents use the field names, only while RUNNING, no key clashes
  test_operations.py     every OpType: plan builds, context covers initial_requires; step names and settings,
                         FORCE_UPDATE's hypertree steps, no upload in CRASH_RESTART
  test_process.py        real processes: start/wait_for_log/stop, old ready lines ignored, early exit, hang,
                         reattach (dead pid, live start), transition table
  test_updates.py        bundles and links, defaults kept, sha256 refusal, reload phases, verify, hypertree round trip
deploy/
  agent.service
.env.example       every setting, with example values; copied to .env
README.md          installing and running it by hand
pyproject.toml     distribution "agent": the agent only; the `agent` command
.gitignore
```

`tests/test_decide.py` covers the force bump and the failed-generation guard. Not written yet: `tests/test_pipeline.py` and `tests/integration/`.

The GitHub repository `TylerGoh/cm` holds the three projects and is the source of truth for code. Its `docs/` folder holds this file, `decisions.md` and `open-items.md`. Every package folder has an empty `__init__.py`.

`docs/metrics-draft.md` holds the parked first draft of performance judging. It is superseded by `performance.py` and is not live code.

Import direction:

```
wire → models, pipeline, state_store → worker_client, elastic → process, updates, feed
  → decide, operations, health, performance, inventory, metrics_exporter → supervisor → app → __main__
```

`config.py` is imported only by `app.py` and `__main__.py`; every other module receives values.

## How it works

- The agent runs as a FastAPI app under uvicorn. The lifespan in `app.py` builds everything and starts the loops.
- `heartbeat_loop` calls `heartbeat_tick()`, then sleeps a fixed `HEARTBEAT_INTERVAL` (a setting; 10s in the examples). No jitter.
- Each heartbeat tick: `_observe()`, then `_sync(observed)` (the heartbeat: send the report, receive the desired state), then `decide()` if idle, then `_start_operation()`, then `_publish(observed)`.
- The agent only acts on a snapshot received this tick, and does nothing if the heartbeat fails. The reply always carries the full desired state; nothing is cached.
- One operation runs at a time, as a background task. `tick` skips `decide` while one is active, and does nothing else about the running operation: it still observes, sends the heartbeat and publishes.
- Operations restart from scratch: no resuming, every step is idempotent, and an interrupted operation is discarded. The agent's memory is persisted only to survive deliberate restarts.
- The agent never rolls back on its own. The cluster manager rolls back by changing the desired state.
- Installing the worker binary is outside the agent's scope. The binary is installed with a default install, so there is always something to run. The default install keeps its configuration as real folders and files under `etc_dir`. The agent manages only what the worker loads: `download` unpacks a version's zip into its own bundle folder and points a configured table of per-file symlinks under `etc_dir` into it, and `start` spawns the worker, which loads whatever the links and the default install's files hold. The agent only ever creates and replaces links; it never moves or deletes a real file. The only version the agent knows is `running_version`, the one the live worker reports. ("Signature version" is the informal name for the same thing; the code always says `running_version`.)
- Artifacts always come from the internal file server: every `artifact_url` in the desired state points there (see "Cluster manager daemon and client"). So does each version's hypertree, a folder the worker generates while loading a version that makes later loads of it faster; UPDATE clears, fetches and uploads it, and FORCE_UPDATE clears and fetches it, as best-effort steps (see operations.py).
- `restart_generation` and `force_update_generation` are per-agent counters. The agent acts when the desired value is higher than the one it last handled, and both sides start at 0.
- CRASHED means the worker has failed: either its pid is gone, or it's alive but hung (it failed `max_failures` liveness checks in a row). Both go through the same crash path.
- The worker outlives an agent restart. The agent's systemd unit has `KillMode=process`, so stopping the agent doesn't kill the worker it spawned. REATTACH relies on this.
- The agent only reports a crash it was alive to see. A worker whose pid disappeared while the agent was down isn't treated as crashed: `reattach` settles the file to STOPPED, since the agent can't know why the worker went away (it may have been stopped by hand).
- Deployment settings come from `config` (see config.py). `SHUTDOWN_GRACE` stays a constant.

## config.py

Code: `config.py` (`AgentConfig`, `config`). Imported as `from config import config`.

- The only source is the `.env` file next to `config.py`, in the agent's project folder. Provisioning (Foreman) writes it. The process environment is ignored (`settings_customise_sources` returns only the init and dotenv sources), so a stray `HOSTNAME` or `PORT` in the environment can't override a value. Keys have no prefix.
- There is no host setting: every service binds `0.0.0.0` (`BIND_HOST` in each service's entry point).
- `WORKER_INITIALIZE_FLAG` is the text that marks the worker's ready line in its program log.
- No defaults, and `extra="forbid"`: a missing or misspelled setting fails startup. A missing file fails with its path.
- `worker_command` is a JSON list (binary first, each flag its own item), at least one item. A single-quoted value may span lines.
- `bundle_symlink_mapping` is a JSON object: file inside a bundle → file path under `etc_dir` where its symlink goes. Links are per file, never per folder.
- `bundle_symlink_default_dir` is a folder under `etc_dir`. Every bundle file not in `bundle_symlink_mapping` is linked there by its name alone, flattened. Empty turns it off (`app.py` passes `None`).
- Settings read by `pydantic-settings` directly are not exported, so the worker never sees them; it inherits the agent's ordinary environment.
- The values are the owner's. `config.py` has a comment per section of settings, not per setting.

## wire.py

Code: `agent/wire.py` (`OpType`, `ProcessStatus`, `AgentStatus`, `DesiredState`, `StepView`, `OperationView`, `HeartbeatReport`).

Placement rule: a type goes in `wire.py` if changing it requires coordinating with the cluster manager. Incoming models are lenient (`extra="ignore"`). Add a separate agent-side model only when its shape actually differs from the wire's.

Class names in `wire.py` are agent-side; only field names and enum values go on the wire.

The heartbeat is `HeartbeatReport` out and `DesiredState` back; the response body is the desired state itself, with no envelope.

`DesiredState` has nine fields. `version` is also the file server's job id: upstream and the file server call a version a job, and the job id is always exactly the string the worker's version endpoint returns. `hypertree_get_url` and `hypertree_upload_url` are where the agent fetches and uploads the hypertree of `version`. The file server has one hypertree URL per job (`/jobs/<job_id>/hypertree`, GET to fetch, PUT to upload), so today both fields carry the same URL. The daemon builds it, so the agent never needs the file server's address or paths.

`OperationView.generation` is the desired generation the operation was started from (`active.desired.generation` in `_operation_view`).

Timestamps are `AwareDatetime` (`OperationView.started_at` and `finished_at`): a value without a timezone fails validation on both sides. The type requires a timezone, not UTC; the agent sends UTC (`datetime.now(UTC)`). The same rule holds for every Pydantic model with a timestamp that crosses a boundary or is persisted: `ProcessMemory.create_time` and every timestamp in the client↔daemon schemas (`cluster_manager/schemas.py`).

`HeartbeatReport` carries the agent's IPv4 address (`ipv4: IPv4Address`, from `config.ipv4`), which is its identity. It is a plain string on the wire. Pydantic rejects a malformed address on both sides and normalises it, so the cluster manager always receives one canonical form, which it stores as a string. The first heartbeat from an unknown IP registers the agent; there is no separate registration message. Hostname and location are not on the wire.

The cluster manager's copy, `cluster_manager/daemon/wire.py` in the `cluster-manager` project, is identical to `agent/wire.py`, so neither side has fields of its own. The two copies live in separate projects, and nothing but review checks that they match (an open item). There is one standard and no `protocol_version`: a wire change is deployed to both sides together, and a mismatch shows up as a failed heartbeat.

`ProcessStatus` is the worker process's status. `ProcessManager.derive_status(responsive)` is the only place it is derived, and CRASHED there includes a hung worker. Every field holding it is named `process_status` and carries that result: `HeartbeatReport.process_status`, `Observed.process_status`, `Conditions.process_status`, and the inventory document's `process_status`. In the report, the observation and the feed, `None` means not attached yet.

## models.py

Code: `agent/models.py` (`HostInfo`, `Observed`, `AgentMemory`, `AgentMemoryChanges`, `ProcessMemory`). The running operation's record is `ActiveOperation` in `supervisor.py`.

`HostInfo(ipv4, hostname, cluster_name)` is this box's identity, a runtime dataclass built once in the lifespan from `config`. All are written upstream by Foreman (`CLUSTER_NAME` is required); nothing in the agent looks them up from the OS (no `gethostname`). A location, or any other per-box value, is an inventory `env:` field in `config.yaml` (see inventory.py). The supervisor gets only `host.ipv4`; `ElasticClient` gets the whole `HostInfo`.

`AgentMemory` is the agent's persisted policy memory: the generation it acknowledged, the counters it handled, the generation it gave up on, and whether it spent its crash restart. It is named apart from `AgentStatus`, the derived label in the report.

`ProcessMemory(pid, create_time, state)` is the persisted process record: the last state the agent wrote and the identity of the process it started. It is named apart from `ProcessStatus`, the status derived from it. `create_time` is an `AwareDatetime`: the process's own creation time from the OS (`psutil`), converted to UTC once when it's read. `_find` converts the live process's time the same way and compares the two, allowing 1s. A naive datetime is rejected when the model is built.

`Observed(process_status, healthy, running_version)` is one snapshot per tick.
- There is no separate `attached` field: `_observe` reads `ProcessManager.attached` and sets `process_status=None` while not attached.
- `healthy` is read from the health monitor once, before any await. The same value is passed to `ProcessManager.derive_status` and sent in the report, so the report can't pair a status with a different health reading. `decide` receives it but must not use it (a convention, not the type).
- `running_version` comes from `WorkerClient.version()` and is only fetched while RUNNING.
- `violations` is not in `Observed`; the performance monitor owns it and `decide` never reads it.

`AgentMemoryChanges` is a `TypedDict` with `total=False` whose keys and types mirror `AgentMemory`. It types the keyword arguments of `Supervisor._set_memory` (`**changes: Unpack[AgentMemoryChanges]`), so a misspelled field or a wrong value type is a type error.
- Every key may be left out, which means "leave this field unchanged". A key that is present must have the field's type, so only `failed_generation` can be set to `None`.
- It is a static check only: `model_copy(update=...)` doesn't validate at runtime.
- `tests/test_models.py` checks that its annotations equal `AgentMemory`'s fields, so the two can't drift apart.

## state_store.py

Code: `agent/state_store.py`. `JsonStore[T: BaseModel]` holds no value; the file is the only copy. `load()` raises `FileNotFoundError` or `ValidationError`, and `save()` is atomic: temp file, fsync, `os.replace`, then an fsync of the folder so the rename itself survives a power loss. It doesn't create the file's folder; that is part of installing the agent. Two stores, each failing differently:

- `AgentMemory`: a missing file means first boot, so build it explicitly and save: generation and both handled counters at 0, no failed generation, `crash_restarted=False`. A corrupt file fails startup on purpose. The supervisor loads it in `__init__` and keeps the in-memory copy (`memory`); `_set_memory` is the only writer, and it saves before updating memory.
- `ProcessMemory`: a missing file is first boot, written as `ProcessMemory(pid=None, create_time=None, state=STOPPED)`. `ProcessManager` reads the file on each use, so a derived status is never based on a stale in-memory copy.

First boot for both is handled by `_ensure_first_boot` in `app.py`, before the supervisor is built. Both paths come from `config`.

The last operation is not persisted (see supervisor.py).

## decide.py

Code: `agent/decide.py`. Settled.

`decide` gets only values. It never sees a manager or a client, never does IO, and never reads `healthy` or `violations`. A hung worker reaches it only as CRASHED. It returns what to do; `operations` says how, and the supervisor says when. `process_status is None` (not attached) means REATTACH.

## pipeline.py

Code: `agent/pipeline.py` (`Stage` with `from_callable`, `Guard` with `from_callable`, `PipelineStep`, `StepRecord`, `Pipeline`). Generic engine; imports nothing from the agent.

- `Stage.from_callable(fn, *, fixed, produces)` derives a stage from a signature. Parameter names are context keys and annotations are their types. `produces` names the return value. `fixed` pins arguments to constants. Every parameter must be plain, required and annotated with a plain class. The produced value is type-checked when the stage runs.
- `Guard.from_callable(fn)` derives a guard from a plain, synchronous function annotated `-> bool`, with the same parameter rules as a stage (names are context keys, annotations their types; no `fixed`, no `produces`). An async function or a missing `-> bool` raises `TypeError` when the guard is built; a non-bool result fails the step when the guard runs. The parameter rules live in one shared helper, `_plain_params`.
- `PipelineStep.when` is an optional `Guard` (`None` = always runs). When the guard returns False, the stage doesn't run and the step is recorded `ok=True, attempts=0`. A guard that raises fails the step (`ok=False, attempts=0`, error `guard <name>: ...`), and a critical step's guard failure stops the pipeline like any critical failure. `attempts == 0` therefore always means the stage never ran.
- `PipelineStep.retries` counts retries after the first try, so a step runs at most `retries + 1` times (default 3, so up to four tries; `retries=0` means one try). A negative value raises `ValueError` when the step is built. Retries run back to back with no delay; a stage that has to wait for something (such as `UpdateManager.verify`) waits inside itself.
- `initial_requires()` is derived, not declared: everything required (by a stage or a guard) that no step produces, plus all finally-step requirements. A finally step therefore can't depend on a produced key.
- `_check_structure()` runs at construction: no key with conflicting types (checked separately for a step's stage requires, stage produces and guard requires, so a merge can't hide a conflict), no key produced twice, every produced key a normal step or its guard requires comes from an earlier critical, unconditional step, and no finally step or its guard requires a produced key. A step with a guard never counts as a producer for later steps, like a non-critical one.
- `_check_context(ctx)` runs at the start of `run`, before any side effect. It rejects missing keys, wrong types, and the caller supplying a key a step produces.
- `run` copies `ctx`, and each produced dict is merged into the copy. `_attempt` evaluates the guard, then tries the stage up to `retries + 1` times, records one `StepRecord` (whose `attempts` counts tries), and returns a bool.
- When a critical step fails, the remaining normal steps are skipped. `failure` is set in one place only, right after that failure, from `step_log[-1]`. Failures of non-critical and finally steps stay in their records only. Callers read `failure`; they never write it.
- Finally steps always run (a `try/finally` in `run`): after success, after a critical failure, and on cancellation. On `CancelledError`, `_attempt` records the step with `error="cancelled"`, the finally steps run within the supervisor's shutdown grace, and the error propagates. A second cancellation while they run stops the remaining finally steps.
- `StepRecord` keeps `name`, `ok`, `attempts` and `error`; `_operation_view` maps it one-to-one into `StepView`.
- Stages that use threads need a real way to cancel, or shutdown waits out the full grace period. No stage uses a thread: the long file work (unzipping, zipping) runs on the event loop in chunks and yields between them.

## Managers (process.py, updates.py)

Code: `agent/process.py`, `agent/updates.py`. Plain attributes, properties and plain methods, no pipeline import. A property is used only when it computes something; a value that would only be returned as-is is a plain attribute.

Both managers are written out in the project. Step names come from method names, so the cluster manager sees `start` and `stop` in `StepView`. Nothing depends on the earlier, private names (`spawn_process`, `kill_instance`).

### ProcessManager

`ProcessManager(worker, store, *, command, console_log_dir, program_log_path, initialize_flag, start_timeout, stop_timeout)`. It owns `ProcessStatus` and holds no status in memory: every call reloads the state file through `JsonStore`. It holds the `WorkerClient` only to call `version()` in `wait_for_version`; every command to the worker goes through `UpdateManager`.

- `attached: bool` is the one piece of `ProcessManager` state held in memory, on purpose. It is false until `reattach` succeeds, and only `reattach` sets it (a convention, not enforced). It describes this agent process rather than the worker, so an agent restart resets it, `_observe` reports `process_status=None`, and the next tick does REATTACH.
- Process identity: `_find(memory)` returns the live `psutil.Process` the memory records, or `None`. A pid whose creation time differs from the stored `create_time` by more than 1s is a reused pid and counts as gone. A zombie counts as gone, and a zombie of ours is reaped (`os.waitpid(pid, WNOHANG)`; a worker found by reattach isn't our child, and init reaps it). `_kill` signals through `psutil`, which re-checks identity, so a reused pid is never signalled.
- `transition_to(state, *, pid=None, create_time=None)` is the only writer of the stored `ProcessMemory`, including when `reattach` settles the file:
  - It loads the file. If the stored state already equals `state`, it does nothing, so a retried step can safely ask again.
  - It raises on any transition outside this table, which holds stored states only, so CRASHED can never be written:

    | From | Allowed to |
    |---|---|
    | STOPPED | STARTING |
    | STARTING | RUNNING, STOPPING |
    | RUNNING | STOPPING |
    | STOPPING | STOPPED |

  - `pid` and `create_time` are accepted only when entering STARTING, where both are required.
  - Otherwise it saves a new `ProcessMemory`, built explicitly (the model is frozen). Entering STARTING records the new process's `pid` and `create_time`; entering STOPPED clears both; any other target keeps them and only changes `state`.
- `derive_status(responsive)` derives the current status from the state file, whether the pid is alive, and `responsive`, the health verdict passed in by the supervisor. It writes nothing.

The worker has two logs:
- The **console log**: the spawned process's stdout and stderr. Each spawn gets its own file in `console_log_dir`, named by the spawn time in UTC (`worker-console-20261104T094512Z.log`), so the newest is the last by name and the path is also in the journal. The agent never reads it and never deletes it; `/tmp` cleaning bounds it.
- The **program log**: the worker's own, ever-growing log at `program_log_path`. The agent never moves or truncates it. Its ready line comes later than the version endpoint answering, and means the worker's metrics can be trusted.

How the steps use `transition_to`:
- `start() -> int`: refuses unless the file says STOPPED, so a stray call can't spawn a second worker. Notes the program log's size, spawns the worker (`posix_spawnp` with the configured command, in its own process group, stdin from `/dev/null`, stdout and stderr to a new console log), and goes to STARTING with its pid and `create_time`, read from the OS right after the spawn. It returns the program log's size from just before the spawn, produced as `log_offset`. It doesn't wait. There is no await between the spawn and the write, so an agent crash leaving an unrecorded orphan would need it to die in that sub-millisecond gap, which is accepted.
- `wait_for_log(log_offset)`: reads the program log forward from `log_offset` with `readline()` (from 0 if the file is now shorter, meaning it was replaced), until a line contains `initialize_flag`, then goes to RUNNING. A half-written line is reread whole on the next poll. If the pid dies first, it raises at once, leaving the file at STARTING (a dead pid there derives CRASHED). On timeout (`start_timeout`) it SIGKILLs the recorded pid, waits for the exit (bounded by `stop_timeout`), and raises, with the same result. `log_offset` lives only in the operation's context and is never stored.
- `wait_for_version()`: used by REATTACH only, for a live worker found in STARTING. A new agent doesn't know where this run's lines begin in the program log, so it waits until the worker answers the version endpoint, then goes to RUNNING. That is earlier than the ready line, which is accepted when reattaching: the first performance window may include a few readings from before the worker was fully ready. Same early-exit and timeout handling as `wait_for_log`.
- `stop()`: a no-op on STOPPED. Otherwise goes to STOPPING, sends SIGKILL to the recorded pid if it's alive, then runs `wait_for_stop`. For a dead or hung worker the file still says RUNNING, so this is the normal RUNNING → STOPPING → STOPPED path. There is no SIGTERM and no escalation: every stop is a kill.
- `wait_for_stop()`: sends nothing. It polls until the recorded pid is gone, then goes to STOPPED, and raises if the pid is still alive after `stop_timeout`. It is run by `stop`.
- `reattach()`: loads the file and checks whether the stored pid is alive. With a dead pid it settles the file to STOPPED through `transition_to`, whatever the file said: STARTING or RUNNING go through STOPPING to STOPPED, and STOPPING goes to STOPPED. With a live pid, or a file that already says STOPPED, it writes nothing. Then it sets `attached`.

`ProcessManager.reattach` returns a `ReattachPlan(wait_for_start, wait_for_stop)` (a frozen dataclass in `process.py`) saying which transition, if any, a live worker was found in. Both are false after settling a dead pid. REATTACH runs `wait_for_version` when `wait_for_start` is true, and the full `stop` when `wait_for_stop` is true: a previous agent may have written STOPPING and died before its kill, so REATTACH kills again rather than only waiting. In `stop`, `transition_to(STOPPING)` is then a no-op.

### UpdateManager

`UpdateManager(worker, *, download_dir, bundle_dir, etc_dir, bundle_symlink_mapping, bundle_symlink_default_dir, hypertree_dir, download_timeout, hypertree_timeout, verify_timeout, verify_interval)`. It is the only manager that sends commands to the worker (through the `WorkerClient`), and the only agent code that talks to the file server, through its own `httpx` client. The constructor rejects a `bundle_symlink_mapping` path or a `bundle_symlink_default_dir` that is absolute or contains `..`.

Artifacts are bundles, and `etc_dir` gets per-file symlinks into them:

```
<bundle_dir>/<version>/...                                one unpacked zip per version ("update_bundle")
<etc_dir>/<target file> -> <bundle_dir>/<version>/<source file>    one symlink per bundle_symlink_mapping entry
<etc_dir>/<default dir>/<name> -> <bundle_dir>/<version>/<any path>/<name>    every other bundle file, with bundle_symlink_default_dir
<etc_dir>/...                                             everything else stays the default install's real folders and files
```

- `download(artifact_url, artifact_sha256, version)`: refuses a `version` that can't be a folder name (the file server's job-id rule). If `<bundle_dir>/<version>` doesn't exist, it streams the zip from the file server into `download_dir` while hashing it, checks the sha256, unpacks it into `<version>.part` and renames that to `<version>`, so a bundle folder only exists once it is complete. Then it links: it first plans every link and checks the plan (the bundle has every mapped source as a file, no two links share a target, which includes two unmapped files with the same name, and nothing at a target path is a real file), so a bad bundle or a real file in the way changes nothing. For each mapping it then creates the target's parent folders if needed and replaces the link atomically: a new link under a temporary name, renamed over the old one, which is an unlink and a symlink with no moment where the target is missing. Only links are ever created, replaced or removed; the files a link pointed at are never touched, and a real file at a target path is refused and left alone. It then removes, best effort, every other symlink under `etc_dir`, wherever it points, since each version brings its own set of files: after a download `etc_dir` holds exactly the bundle's links (2026-11-05). This runs after the new links are in place, so a planned link is never missing. Real files and folders stay; a link to a folder is removed as a link and not followed. Last, it removes every other bundle, best effort, which is safe because no link points into them any more. It produces nothing. Repeating it fetches nothing and relinks to the same bundle. Fetching and unpacking are bounded together by `download_timeout`.
  - The links are swapped one by one, not as a set: an agent that dies part-way leaves some links on the new bundle and some on the old, until the next `download` relinks them all.
  - A running worker that reads files through `etc_dir` after the links switch sees the new files before `reload`. That is inherent in loading from the linked location.
  - Unpacking drops Unix permissions, like `zipfile.extractall`.
- `clear_hypertree()`: empties `hypertree_dir`. Loading a version makes the worker generate a hypertree as a multi-tier folder named by a hash, and that name can't be read any other way, so the folder is cleared before the load and the only folder left afterwards is the one the worker generated. Safe while the worker runs.
- `fetch_hypertree(hypertree_get_url, version)`: GETs the version's hypertree zip and unpacks it into `hypertree_dir`, with its `<hash>/` folder intact. A 404 (none yet) returns quietly, and the worker then generates one while loading; any other failure raises. Bounded by `hypertree_timeout`.
- `reload()`: calls the worker's `reload_phase1` then `reload_phase2`, back to back, in one step, so a retry repeats both. It only passes once the worker is running the new code; from then on the worker reports the new version.
- `verify(version)`: polls `version()` until the worker reports the expected version, or `verify_timeout` passes, then raises. Answering the version endpoint at all means the worker is healthy, so there is no separate health call. UPDATE is the only recipe that runs it, with `retries=1`, so a failing verify step takes up to `2 × verify_timeout`.
- `upload_hypertree(hypertree_upload_url, version)`: zips the one folder in `hypertree_dir` (with its own name as the top entry, so whoever unzips it gets `<hash>/...` intact) and PUTs it. Anything other than exactly one folder means the clear didn't happen, so it raises rather than guess. A 409 (the job already has one) counts as success; any other failure raises. Bounded by `hypertree_timeout`.
- Unzipping and zipping run on the event loop in 1 MiB chunks, yielding between them, so heartbeats keep going and cancellation works.

`UpdateManager` has no `running_version`; anything that needs the live version calls `WorkerClient.version()` itself. It holds no "updating" flag either: whether an update is running is derived by the supervisor (see feed.py).

### Process state rules

- CRASHED is never stored, and is only ever derived by `ProcessManager.derive_status`:
  - STARTING or RUNNING in the file with a dead pid: CRASHED.
  - RUNNING in the file, pid alive, `responsive is False`: CRASHED (hung).
  - STOPPING in the file with a dead pid: STOPPED.
- `derive_status` is only called while attached, and `reattach` has settled any dead pid before `attached` is set. So a dead-pid CRASHED only ever describes a death during this agent's lifetime.
- Leaving CRASHED happens through CRASH_RESTART's `stop` and `start`, which go through `transition_to`. A hang is never a transition.
- The stop step writes STOPPING before killing and STOPPED after the exit is confirmed. It always sends SIGKILL, so a worker that is slow to exit can't hold it up; `stop_timeout` only bounds the wait for the exit after the kill. It is idempotent against a half-stopped worker, and is a no-op when the pid is already gone.
- STARTING with a live pid must never be left in the file with no operation running. REATTACH waits it out with `wait_for_version`, and both waits kill the process on timeout. STARTING with a dead pid derives CRASHED, so a failed start goes through the crash path.
- A stop that fails (the process outlives the kill and `stop_timeout`) leaves the file at STOPPING with a live pid, for which `decide` does nothing. A SIGKILL not ending a process is rare enough that this is accepted; it doesn't count as a crash.
- Live worker queries use a short timeout and return `None` instead of raising.

What gets written when the worker crashes or hangs, in order:
1. Nothing, when CRASHED is first derived. The state file keeps the last state the agent wrote (RUNNING), and every read derives CRASHED again from the file, the pid and the health verdict.
2. `AgentMemory.crash_restarted=True`, saved by `_start_operation` when `decide` returns CRASH_RESTART, after `plan()` has built the recipe and before the task starts. This is the first write: a fact about the agent's own decision.
3. The process file, only through CRASH_RESTART's `stop` (RUNNING → STOPPING → STOPPED) and `start` (STOPPED → STARTING → RUNNING), via `transition_to`.

The crash budget is `crash_restarted`: one automatic CRASH_RESTART, then CRASHED_HOLD. It is a convenience, not a guarantee, and it resets when an operation downloads a new bundle into place: a successful UPDATE or FORCE_UPDATE clears it. START, RESTART and CRASH_RESTART don't.

In CRASHED_HOLD no operation runs, so the file keeps saying RUNNING and every read keeps deriving CRASHED. An agent restart ends the hold for a dead worker, and that is accepted: `reattach` settles the file to STOPPED, the crash isn't reported again, and `decide` runs START if `desired_running`, unless the current generation already failed (`failed_generation`), in which case the worker stays STOPPED until a new generation: a force bump, or a stop and start. START doesn't reset the budget, so if `crash_restarted` was set, the worker's next crash goes straight back to CRASHED_HOLD. A hang verdict lives only in the health monitor's memory, so a hung (live) worker reads RUNNING after an agent restart, or after an operation such as a FORCE_UPDATE, until `max_failures` checks fail again. That delay is accepted.

## operations.py

Code: `agent/operations.py` (the guards `WAIT_FOR_START` and `WAIT_FOR_STOP`, `_Stages`, `Plan`, `plan`). All seven recipes are written out.

Stages are built with `Stage.from_callable` from the manager methods (see pipeline.py). `fixed` pins arguments to constants, so two stages built from the same method can be told apart in the step log. Every step is written out as `PipelineStep(...)`, with no wrapping helpers.

| Op | Steps | Finally | Context |
|---|---|---|---|
| REATTACH | reattach, wait_for_version (when, retries=0), stop (when) | – | {} |
| UPDATE | download, clear_hypertree (critical=False), fetch_hypertree (critical=False), reload, verify (retries=1), upload_hypertree (critical=False) | – | artifact, hypertree_get_url, hypertree_upload_url |
| FORCE_UPDATE | download, clear_hypertree (critical=False), fetch_hypertree (critical=False) | – | artifact, hypertree_get_url |
| START | start (retries=0), wait_for_log (retries=0) | – | {} |
| CRASH_RESTART | stop, start (retries=0), wait_for_log (retries=0) | – | {} |
| RESTART | stop, start (retries=0), wait_for_log (retries=0) | – | {} |
| STOP | stop | – | {} |

- `start`, `wait_for_log` and `wait_for_version` never retry. Repeating a spawn could start a second worker, and the waits kill the process when they time out, so a retry could only wait out `start_timeout` again on a dead pid. Other steps keep the default (3), except UPDATE's `verify` (1).
- `artifact` is `artifact_url`, `artifact_sha256` and `version` from `desired`. `hypertree_get_url` and `hypertree_upload_url` also come from `desired`. Within a recipe, `start` produces `log_offset` for `wait_for_log`, and `reattach` produces `reattach_plan` for the REATTACH guards; nothing else is passed between steps. `download`, UPDATE's `verify` and the hypertree steps read `version`.
- The hypertree steps only make loading faster, so they are non-critical: a failure stays in its `StepView`, never sets `failure` or `failed_generation`, and the update goes on. `clear_hypertree` and `fetch_hypertree` run before `reload` so the worker loads with only the fetched hypertree (or none) in place; `upload_hypertree` runs last, after `verify` has confirmed the worker loaded the version. A failed clear makes the upload refuse (more than one folder). FORCE_UPDATE clears and fetches the hypertree too, so the CRASH_RESTART that loads the new bundle next starts from the stored hypertree. Nothing uploads a hypertree after a CRASH_RESTART: it would only speed up later loads, which isn't worth risking a recovery for.
- Conditional steps use `when=Guard`. Guards are module-level functions in `operations.py`, turned into `Guard`s with `Guard.from_callable`. `plan()` never branches on runtime state: it is built from `desired` alone, and anything only known at run time (such as what `reattach` found) goes through a produced context key read by a guard.
- `Plan.context` is a `Mapping[str, object]`, not a `dict`, so the context dicts type-check.
- FORCE_UPDATE only runs when the worker is CRASHED or STOPPED. From CRASHED, the worker stays CRASHED (or, if it was hung, stays alive until health fails again), the crash budget is wiped, and a later tick does a CRASH_RESTART, which loads the newly linked bundle with the fetched hypertree, if the file server had one. From STOPPED, `decide` checks a pending force bump before `failed_generation`, so it also frees a worker stopped after a failed start; a later tick does a START if `desired_running`. The cluster manager accepts a force bump for either status (2026-11-05).
- CRASH_RESTART has the same steps as RESTART. It differs only in when `decide` picks it (CRASHED rather than a restart bump) and in using the crash budget. Its `stop` matters because CRASHED includes a live, hung worker; `stop` is a no-op when the pid is already gone. There is no `crash_evidence` step for now, so nothing captures a hung worker's state before the kill. If it comes back, it goes first and non-critical, so it can read the live process and its failure can't block the restart.
- START, CRASH_RESTART and RESTART spawn the worker, which loads whatever is linked plus the default install's files, not necessarily the desired version, and runs whatever version that is.
- Only UPDATE verifies the version. START, CRASH_RESTART and RESTART end with `wait_for_log`, their only check: they succeed once the worker logs that it is ready. A timeout kills the process and fails the operation, and the dead pid in STARTING then derives CRASHED. `decide` handles CRASHED before it checks `failed_generation`, so a failed START or RESTART is followed by one CRASH_RESTART if budget is left, and a failed CRASH_RESTART ends in CRASHED_HOLD. A running version that differs from `desired.version` becomes an UPDATE on a later tick through `decide`. A worker that starts but stops answering its version endpoint is caught afterwards by the health monitor as hung, and goes through the crash path.
- Checks run in this order: `Stage.from_callable` and `Guard.from_callable` check each signature, `PipelineStep` checks `retries`, `Pipeline.__init__` checks the stages and guards fit together, and `run` checks the initial context. Produced values are type-checked when each stage runs.
- No recipe sets an "updating" flag; it's derived from the operation type. Any flag a recipe does set must be reset in `finally_steps`.
- Behaviour knobs go in the context, and deployment settings in the manager constructors. A knob only becomes its own OpType when `decide` has to choose it.
- Every operation must end. Each step that can hang is IO and is bounded by its own timeout (`download`, `fetch_hypertree`, `reload` (each phase), `verify`, `upload_hypertree`, `wait_for_log`, `wait_for_version`, and the wait for the exit in `stop`), and a timeout is a step failure, reported through `failure` and the step's `error` like any other (or, for a non-critical step, through its `error` only). The cluster manager sets no deadline, so an unbounded step would keep the agent busy, and ignoring every desired state, indefinitely.

## supervisor.py

Code: `agent/supervisor.py` (`SHUTDOWN_GRACE`, `UPDATING_OPS`, `ActiveOperation`, `_operation_view`, `Supervisor`). Written out in full.

The supervisor holds the health monitor to read `healthy` once per tick in `_observe`, the performance monitor to read `violations` and `metrics_available` for the report, the feed only to write it, and the `WorkerClient` only to call `version()` in `_observe`. It doesn't know the inventory exporter. Its constructor takes the agent's `ipv4` first, which `_report` puts in every `HeartbeatReport`.

- `ActiveOperation` is one flat, frozen dataclass holding everything about the running operation: `op_id`, `type`, `started_at`, the `desired` state it was started from, its `pipeline` and its `task`. There is no separate operation-identity class.
- `tick`: `_observe`, `_sync`, then `decide` and `_start_operation` only when no operation is active and the heartbeat returned a desired state, then `_publish`. Nothing watches a running operation from `tick`; it ends through `_drive` and `_finish`.
- `_observe`: gathers one snapshot and applies no rules of its own. It reads `health.healthy` once, before any await, gets the status from `ProcessManager.derive_status(healthy)` while attached (`None` otherwise), and fetches `running_version` only when the status is RUNNING.
- `_sync`: sends `_report(observed)`. On any exception it increments `_sync_failures`, logs only the first failure, and returns `None`. On recovery it logs the count. It returns `None` for a stale generation, and saves a newer one with `_set_memory`.
- `_report`: returns a `HeartbeatReport` built only from `ipv4`, the snapshot (including `observed.healthy`), `memory`, `_active`, `last_operation`, `performance.violations` and `performance.metrics_available`. `current_operation` comes from `_operation_view(active, finished_at=None, failure=None)`.
- `_agent_status(observed)`: BUSY if active, CRASHED_HOLD if CRASHED and `crash_restarted`, otherwise IDLE.
- `_start_operation`: `plan()` first. Then, for CRASH_RESTART only, save `crash_restarted=True`. Then `create_task(_drive(context))` and set `_active`. `_drive` takes only the context and reads everything else from `_active`, which is set before the task first runs. A `plan()` error is not a failed operation: it isn't caught, so it sets no `failed_generation` and no `last_operation`, the budget stays unspent, and the error ends the tick loop (`_exit_if_failed` sends SIGTERM and systemd restarts the agent). No operation is active at that point, so shutdown has nothing to cancel. Saving the budget before `create_task` means an operation never runs without it recorded: if the save fails, nothing starts.
- `_drive`: logs the starting context, which is enough to replay it in a test. Runs the pipeline and works out the failure: the pipeline's own `failure` when it set one, otherwise the message of an exception `run` raised (for example from `_check_context`). It never writes `pipeline.failure`. It logs failed steps and calls `_finish(active, failure)`. A `CancelledError` isn't caught, so a cancelled operation never reaches `_finish` and nothing is logged for it: no "operation finished" line and no failed-step warnings. That is intended; the agent's own shutdown lines already show it was stopping.
- `_finish(active, failure)`: sets `last_operation` for every op, including REATTACH, and logs it as one JSON line (`log.info("operation finished: %s", view.model_dump_json())`). It collects the memory changes in a `changes: AgentMemoryChanges` dict. REATTACH changes nothing. On success, RESTART marks the restart handled, and UPDATE and FORCE_UPDATE clear `crash_restarted`. UPDATE doesn't mark a pending restart handled, so when both are pending, RESTART runs after the UPDATE. On failure, `failed_generation = desired.generation`. FORCE_UPDATE always marks its counter handled. Then it clears `_active` and republishes the last snapshot, so `busy` and `updating` drop at once.
- `_set_memory(**changes: Unpack[AgentMemoryChanges])`: the only writer of `AgentMemory`. It copies the current memory with only the given fields replaced (`model_copy(update=changes)`), saves it, and only then replaces `memory`, so a failed save leaves file and memory on the old value. Every caller (`_sync`, `_start_operation`, `_finish`) is type-checked against `AgentMemoryChanges`.
- `last_operation` is in memory only. It starts as `None` and is `None` again after an agent restart. Keeping operation history is the cluster manager's job; the report only tells it how the most recent operation ended. The journal line from `_finish` is for diagnosis on the box only.
- `_operation_view` is a module function in `supervisor.py`, so that `wire` never imports `pipeline`.
- `shutdown`: cancels the active task and waits `SHUTDOWN_GRACE` (20s) for the finally steps, then cancels again so the remaining ones stop. An interrupted operation is discarded, not finished, and isn't logged.

Every state change in `tick` is synchronous (`_set_memory`, `_start_operation`, `_publish`), so cancelling the tick loop can only land at an `await` in `_observe` or `_sync`, where nothing is half-written.

## Monitoring

Metrics, performance and getting data into Elastic are separate concerns. Metrics are the raw readings (`worker_client.py`). Performance is the agent's judgement over them (`performance.py`). Exporting the readings to Elastic is the metrics exporter's job (`metrics_exporter.py`).

Health is read directly: the worker's version endpoint is the cheapest ping, and answering it is the liveness check (the process is answering), so a dependency outage doesn't fail it. The worker only stops answering while doing blocking IO during an operation the agent drives, never on its own. Performance is inferred: the agent judges from raw numbers against its own thresholds. That's why `healthy` and `violations` are separate report fields.

### feed.py

Code: `agent/feed.py` (`Conditions`, `ConditionFeed`). `Conditions(process_status, busy, updating, running_version)`. The supervisor writes once per tick in `_publish`; the monitors and the inventory exporter read. The feed breaks the construction cycle: the supervisor needs the monitors' values, and the monitors need the supervisor's conditions.

- The initial value has `process_status=None` and everything else empty, so the monitors stay inactive until the supervisor's first `_publish`.
- No locking: everything runs on one event loop, `publish` and `current` are synchronous, and `Conditions` is frozen.
- `process_status` is the observed status from `ProcessManager.derive_status`, so CRASHED includes a hung worker.
- `busy` and `updating` are both derived from `_active` in `_publish`, never stored. `busy` is true while any operation runs. `updating` is true while the operation is in `UPDATING_OPS` (UPDATE, FORCE_UPDATE). The supervisor decides which operations count as updating, so the operation type never reaches the feed. Being derived, neither can be left stuck at `True` by a crash, a cancellation or a missed reset.
- `running_version` is only fetched while the process is RUNNING, so it's `None` otherwise.
- `_publish` runs every tick, even when the heartbeat failed, and `_finish` publishes so `busy` and `updating` drop as soon as an operation ends. If the heartbeat loop stalls, readers keep seeing the last conditions.

### health.py

Code: `agent/health.py` (`HealthMonitor`). `HealthMonitor(worker, feed, *, interval, max_failures)` runs its own loop with a plain `sleep(interval)`; exceptions are logged and the loop continues. `interval` and `max_failures` come from `config`.

- Active while `process_status` is RUNNING or CRASHED and no operation runs (`busy` is false). Otherwise it clears its value to `None` and resets its failure count.
  - Staying active while CRASHED keeps a hang verdict from resetting itself and flapping the status back to RUNNING.
  - Going inactive while busy means a reload, start or restart can't build up failures. It covers every pause, since the worker only stops answering during operations the agent drives.
- A check fails when `WorkerClient.health()` returns `False` (answered with an error) or `None` (didn't answer). A passing check resets the count and reports `True`. A failure keeps the last value until `max_failures` fail in a row, then reports `False`.
- `healthy` is `None` (no data yet), `True`, or `False`. There's no staleness check. It lives in memory only, so an agent restart starts it again at `None`.
- The monitor only judges; it never sets a process status. The supervisor passes `healthy` to `ProcessManager.derive_status`, where `False` makes a live worker count as CRASHED (hung). A hung worker that answers again goes back to `True`, and so to RUNNING. `max_failures × interval` decides how long a stall counts as a hang, so it must be long enough not to kill a worker under heavy load.

### worker_client.py

Code: `agent/worker_client.py` (`MetricSource`, `MetricType`, `WorkerMetrics`, `METRIC_SOURCES`, `COUNTER_FIELDS`, `GAUGE_FIELDS`, `WorkerClient`). The command names and metric names are mock until the real worker's are known.

- The whole worker contract is one generated block (`# BEGIN generated: worker contract` to `# END generated: worker contract`): `VERSION_COMMAND` (`show_version`), `RELOAD_PHASE1_COMMAND`, `RELOAD_PHASE2_COMMAND`, one `MetricSource` constant per metric command (`COUNTERS_ENDPOINT`), and `WorkerMetrics`. `prepare.py` writes it from `config.yaml`'s `worker:` section; nothing else in the file changes when the contract does (see "Packaging and deployment").
- Every command is a format string: `_url()` calls `command.format(config=settings)`, where `settings` is the agent's config (`WorkerClient(..., settings=config)`), so `reload_app_phase2{config.etc_dir}` is sent as `reload_app_phase2/srv/worker/etc`. A command without fields is unchanged; `{{` and `}}` are literal braces. `prepare.py` refuses a `{config.x}` naming a setting the agent doesn't have.
- `WorkerMetrics` has three cumulative counters, all from the `counters` command (mock names): `requests_out_total` (sent out), `requests_old_failed_total` (failed because too old) and `requests_out_lagged_total` (failed because lagged). There are no gauges.
- Queries never raise: `version()`, `health()` and `metrics()` return `None` on failure. `version()` and `health()` use the same endpoint: `version()` reads its `version` field, and `health()` returns `True` when it answered with success, `False` when it answered with an error, and `None` when it didn't answer. Cancellation always propagates.
- `reload_phase1()` and `reload_phase2()` are GETs with no body, and raise, because a step uses them. Each is synchronous on the worker: it blocks until that phase is done, so a success means the phase worked and there is nothing to wait for afterwards (confirmed by the owner). An HTTP error becomes a `RuntimeError` carrying the command, the status and the first 200 characters of the body, since the step record keeps only the error string. A network failure or timeout propagates as the `httpx` exception.
- Every request URL is the base URL with a command appended as-is: `base_url + command`, nothing added between them. The base URL (`config.worker_url`) ends where the command name goes, typically in a query such as `http://127.0.0.1:9000/api?command=`, so the commands have no leading slash, and `prepare.py` refuses one that has. `WorkerClient` keeps the base URL itself and builds each URL in `_url(command)`; its `httpx` client has no `base_url`, because `httpx` always adds a `/` to one, which would turn `...?command=` into `...?command=/show_version`. Nothing checks the base URL's shape.
- The constructor is `WorkerClient(base_url, *, timeout, reload_timeout, transport)`. Each reload phase uses its own, longer `reload_timeout`; every other call uses `timeout`. `transport` is for tests (`httpx.MockTransport`).
- One `WorkerClient` is built in the lifespan and passed directly to `ProcessManager`, `UpdateManager`, `HealthMonitor`, `PerformanceMonitor` and the `Supervisor`, sharing one connection pool.
- Counters are `float`. Gauges are `float | str`: a number becomes a float, and a string (`"5d 18h"`) is kept as the worker sent it and exported as it is. Only counters feed `performance.py`.
- Every `WorkerMetrics` field declares three things: a `MetricType` (`COUNTER` or `GAUGE`), a `MetricSource` (the command that returns it), and its name in that command's answer (`validation_alias`, a real Pydantic parameter). `MetricType` and `MetricSource` are plain metadata that Pydantic ignores.
- A metric has exactly two names. The intake name is the worker's JSON key (`requests.out.total`), the `validation_alias`, used only to read the worker's answer. The field name (`requests_out_total`) is its name everywhere after that: in the agent's code and in Elastic's metrics documents. There is no third, Elastic-only name.
- `_derive()` runs at import. It builds `METRIC_SOURCES` (command → names in its answer), `COUNTER_FIELDS` and `GAUGE_FIELDS` (agent-side field names), and raises `TypeError` if a field lacks a `MetricSource`, a `MetricType` or a string alias, or if an alias is used twice.
- An endpoint may serve both counters and gauges; each field declares its own type.
- `WorkerMetrics` describes the worker only. What the agent does with the readings is decided in `performance.py`, not here.

How `metrics()` works:
- It fetches every endpoint in `METRIC_SOURCES` concurrently with `gather(..., return_exceptions=True)`. It's all or nothing, so one reading describes one instant.
- Every metric in an answer is wrapped, `{"value": x}`; it takes `x` for each declared name only. An entry that isn't an object with a `value` key logs an error and returns `None`. If any names are missing, it returns `None`, and logs the endpoint and the names once: not again until a reading has every name, which it logs too.
- `WorkerMetrics.model_validate(values)` renames the dotted names to field names through the aliases.
- A network failure returns `None` silently; a validation failure logs and returns `None`.
- Downstream, the reading is flat and dot-free, so it exports cleanly.

### performance.py: PerformanceMonitor

Code: `agent/performance.py` (`MetricsReading`, `counter_reset`, the user block with `judge`, `PerformanceMonitor`). `PerformanceMonitor(worker, feed, *, interval, window_seconds, max_failures)`, all from `config`; the constructor raises unless `0 < interval < window_seconds` and `max_failures >= 1`.

The judgement is the owner's: `judge(oldest, newest) -> list[str] | None`, with its helpers and limits, written on the box between `# BEGIN user: judge` and `# END user: judge`. The repo ships the block commented out, so an agent without it fails each full window with `NameError` (logged, no verdict). `docs/worker-contract.md` has the pattern. The window, the gate and `metrics_available` don't depend on the fields.
- The counters are cumulative, so the amount over the window is the newest reading minus the oldest. Missed readings inside the window don't matter, and there are no bins.

The window:
- The monitor takes one reading every `interval` and keeps them in a deque. It keeps the newest reading at or before the window's start, so the judged span is at least `window_seconds` (at most one interval more).
- There is no verdict (`None`) until the span covers a full window.
- A counter that went down means the worker restarted: the window starts again from that reading.
- Active only while the worker is RUNNING and no operation runs. Otherwise it forgets everything (readings, verdict, failure count), so every verdict describes one uninterrupted run of one version. The gate is checked again after `metrics()` returns, since an operation can start during the read and an in-place reload restarts nothing.

Nothing is sent to Elastic from the monitor; the metrics exporter sends the readings, taking its own.

`violations` stays three-valued: `None` = no verdict, `[]` = ok, list = problems. Each problem is a free-text message for humans (for example "error rate 6.0% > 5%"). Nothing branches on which check failed, so a violation carries no check key.

`metrics_available` tells the cluster manager whether the agent can read the worker's metrics at all, so a broken metrics endpoint doesn't silently turn performance judging off:
- The monitor counts consecutive `metrics()` calls that return `None`, for any reason (network, missing names, validation). After `max_failures` in a row (`config.max_metrics_failures`, 3) it reports `False`. One successful call resets the count and reports `True`.
- Like the health monitor, it resets to `None` and clears the count whenever it goes inactive (an operation runs, or the worker isn't RUNNING), so an update can't build up failures.
- At `False`, `violations` also becomes `None`: with no data there is no verdict.
- It is a flag only. It doesn't say which endpoint or names failed: whoever is alerted looks at the box, where `metrics()` logs the details.
- The agent never alerts. It only reports; the cluster manager decides (see "Rules for the orchestrator").

## Exporting

### elastic.py

Code: `agent/elastic.py` (`ElasticClient`). `ElasticClient(base_url, host, *, cert_path, api_key, metrics_index, host_index, timeout)`.

- Two indexes. `index_metrics(doc)` writes to the metrics index (the metrics exporter's documents). The host index holds each host's current documents: `put_host(key, doc)` writes the host's current document under the fixed id `<ip>_<key>`, replacing the last one; `delete_other_host_docs(keep)` deletes this host's documents (matched on `ip`, or `ipv4` for documents written before the rename) except the kept keys and any with an `event` field (the `agent_started` records older agents wrote; they're deleted by hand), with `delete_by_query`. Callers never see index names or ids beyond the key. The API key needs `index` and `delete` on the host index.
- Every document gets the host's identity added here: `hostname`, `ip` (the agent's IPv4 address) and `cluster_name` from `HostInfo`. Callers never add them.
- It wraps the official client, `AsyncElasticsearch(base_url, ca_certs=cert_path, api_key=api_key, request_timeout=timeout, max_retries=0, retry_on_timeout=False, node_class="httpxasync")`: async, so a slow Elastic never blocks the event loop, and on httpx, so there's no aiohttp. One attempt per document. The client's major version (`elasticsearch>=8.13,<9`) must match the cluster's. `client=` replaces it in tests.
- TLS is verified against the CA certificate in `elastic.cert`. A missing certificate fails agent startup (`FileNotFoundError`), since it's a configuration error rather than an Elastic outage.
- Every request authenticates with an Elastic API key (the client's `api_key=`). `api_key` is the encoded key Elastic returns when the key is created (base64 of `id:key`), sent as-is. It comes from `config.elastic_api_key`, a `SecretStr`, so it never shows in a repr or a log; `app.py` unwraps it with `get_secret_value()` only where it builds the client. A wrong or revoked key is a 401, which is an ordinary write failure: logged, never raised, and the agent keeps running.
- Awaited directly by its callers. It never raises (cancellation still propagates). Each index logs its failure on `agent.elastic` when it starts and again whenever it changes, and logs the recovery. `describe()` makes the line: Elastic's status, error type and reason (`HTTP 400 document_parsing_exception: failed to parse field [uptime] of type [float]`) with a hint for 401, 403 and 404; a TLS failure (check `ELASTIC_CERT_PATH`); a timeout; a refused connection with its underlying cause; or a server that isn't Elasticsearch or doesn't match the client's version. Never the document or the key. There's no queue and no log handler: Elastic receives data documents only, and logs go to the journal. Keep its timeout short, since the start record is awaited during startup.

### metrics_exporter.py

Code: `agent/metrics_exporter.py` (`DOCUMENT_KEYS`, `MetricsExporter`). `MetricsExporter(worker, elastic, feed, *, interval)` runs its own loop at a steady cadence, `interval` from `config.metrics_export_interval`. Like the inventory exporter, it judges nothing and nothing reads it.

- Each step reads `Conditions`. Unless the worker is RUNNING it does nothing (the inventory records the status). Otherwise it calls `WorkerClient.metrics()` and, when that returns a reading, sends one document to the metrics index: `@timestamp` and `at` (the same UTC time; `@timestamp` is Elastic's time field, for data views), `running_version` and `updating` from the feed, then every `WorkerMetrics` field under its field name (`reading.model_dump()`), plus the host fields `ElasticClient` adds.
- Field names are the names in Elastic; the worker's own (dotted) names never leave the agent, so Elastic never reads `requests.out.total` as nested objects.
- Counters are sent as the worker reports them, cumulative since it started; a dashboard derives rates. Gauges are the current value.
- A failed reading sends nothing: the performance monitor already reports `metrics_available`.
- It takes its own readings rather than sharing the performance monitor's, so neither depends on the other's cadence or gate. Exporting continues during an operation as long as the worker is RUNNING and answers; `updating` marks those documents.
- `DOCUMENT_KEYS` (`@timestamp`, `at`, `running_version`, `updating`, `hostname`, `ip`, `cluster_name`) are taken: the constructor refuses a `WorkerMetrics` field with one of those names, and so does `prepare.py`.

### inventory.py

Code: `agent/inventory.py` (`INVENTORY_FIELDS`, `InventoryExporter`). `InventoryExporter(elastic, feed, *, intfs, interval, require_running)` runs its own loop at a steady cadence. It is not a monitor: it judges nothing and nothing reads it. It depends only on the feed, the Elastic client and `config`.

- Each step reads `Conditions` and keeps this host's documents in the host index current, writing them all (`put_host`) and then deleting every other document of the host (`delete_other_host_docs`): an interface no longer in `INTF`, and records from before the fixed ids. Writing first means the host is never missing from the index. The host fields `ElasticClient` adds go on each.
  - One inventory record per interface in `intfs` (`config.intf`, the required `INTF`, a JSON list of at least one), key `intf_<intf>`: `at`, `kind: inventory`, `intf` and `INVENTORY_FIELDS`.
  - One state document, key `state`: `at`, `kind: state`, then the desired state (`desired`, the last one the supervisor accepted, kept through failed or stale heartbeats) and the observed state (`observed`, the `HeartbeatReport` built when the feed is published), each sent flat: one top-level field per value, `desired_<name>` and `observed_<name>` (`desired_running` stays as it is), without `current_operation`, `last_operation` and the report's `ipv4`. Then `observed_updating` and `observed_busy` from the feed. Every heartbeat value is `None` before the first heartbeat.
- `INVENTORY_FIELDS` is a generated block, a dict written by `prepare.py` from `config.yaml`'s `inventory:` fields, each an agent setting read straight from `config` (`"rack": config.rack`). A new setting is declared in the generated block in `config.py` and listed in the one in `.env.example`, so a missing key fails startup. `prepare.py` checks the fields (snake_case, not repeated, not one of `at`, `kind`, `intf`, `hostname`, `ip`, `cluster_name`); the agent doesn't check them again. With no fields it is `{}`.
- Values are sent JSON-ready: enums by value, paths as text.
- It imports `config`, so it has no unit tests (they would need a full `.env`); devenv runs it.
- `running_version` is `None` unless the process is RUNNING, since that's the only time the supervisor fetches it.
- With `require_running`, it sends nothing unless the process is RUNNING. The value comes from `config`.
- The data is at most one heartbeat tick old. `interval` (from `config`) should be at least one heartbeat tick; shorter only sends duplicates.

## cluster_manager_client.py

Code: `agent/cluster_manager_client.py` (`ClusterManagerClient`). `heartbeat(report) -> DesiredState` POSTs the report to `/heartbeat` with a 5s timeout (`HEARTBEAT_TIMEOUT`). It raises on any error, including a refused heartbeat (403 for a decommissioned agent), and validates the response body directly with `DesiredState.model_validate_json`. It has no `register`: the first heartbeat registers the agent.

The agent identifies itself to the cluster manager by its IPv4 address, `config.ipv4`. Provisioning writes the address to the agent's `.env`. `app.py` passes `host.ipv4` to the `Supervisor` constructor, which puts it in every `HeartbeatReport`. Who may join is enforced by an external IP whitelist.

## app.py and __main__.py

Code: `agent/app.py`, `agent/__main__.py`. `app` is what runs; `__main__` is how the process is set up around it.

Every deployment setting in both files is read from `config`.

`__main__.main()` is the `agent` console script, which systemd also runs:
- Configures logging first (`logging.basicConfig`, level and format from `config`), before importing `agent.app` and before uvicorn starts, so it covers uvicorn's own startup lines and any error before the lifespan runs.
- Runs `uvicorn.run(app, host=BIND_HOST, port=config.port, lifespan="on", log_config=None, timeout_graceful_shutdown=5)`, with `BIND_HOST = "0.0.0.0"`.
  - The app object, not an import string, so uvicorn can't run `workers > 1` or `--reload`. There is exactly one supervisor per box.
  - `lifespan="on"` makes a lifespan error, such as a corrupt state file, stop uvicorn instead of serving without an agent.
  - `log_config=None` keeps uvicorn from replacing the logging set up above; its records go through the root handler.
  - uvicorn owns SIGTERM and SIGINT. Its shutdown runs the lifespan exit.

Logging setup stays out of `app.py`: importing `agent.app` (for example to test the lifespan) must not change global logging.

The lifespan, startup half (before `yield`):
1. Build the stores and run `_ensure_first_boot`.
2. Build `HostInfo` from `config`, then `ConditionFeed`, `WorkerClient`, `ElasticClient`, `ClusterManagerClient`, `ProcessManager`, `UpdateManager`, `HealthMonitor`, `PerformanceMonitor`, `InventoryExporter`, `MetricsExporter` and the `Supervisor`. Each object gets only what it uses.
3. Start explicit tasks (not a TaskGroup): `heartbeat_loop`, `health.run()`, `performance.run()`, `inventory.run()`, `metrics_exporter.run()`.
   - The tick task has `_exit_if_failed`: if it ends with an exception, it logs and sends the process SIGTERM, so uvicorn shuts down and systemd restarts the agent.
   - The other tasks have `_log_if_failed`: a crash is logged and the supervisor keeps running.

The shutdown half (after `yield`, in `finally` so it also runs when the lifespan is left by an exception), in order:
1. Cancel the tick task and wait for it.
2. `supervisor.shutdown()`.
3. Cancel the monitors and the exporters, and wait for them.
4. Close the update manager's file-server client, then the worker, Elastic and cluster manager clients. Nothing uses the `WorkerClient` by then, since the tick loop, the operation and the monitors have finished.

The agent serves no routes: the cluster manager never calls it, and the heartbeat is the only status channel. uvicorn runs the lifespan before it binds the port, so a second agent started by hand runs its first tick before failing to bind; systemd prevents that in practice.

The file reads and fsyncs in `JsonStore` and `ProcessManager.derive_status` block the event loop, which also delays HTTP requests. They are small.

## Packaging and deployment

Code: each project's `pyproject.toml`, `.gitignore`, `.env.example`, `README.md` and `deploy/`; `README.md`, `prepare.py`, `config.example.yaml`, `check_wire.sh` and `tools/make_patch.py` at the top of the repository.

The repository holds three separate projects, one per service. Each is installed and deployed on its own, with its own dependencies, virtualenv, settings file and systemd unit:

```
README.md            the whole flow: release, prepare, settings, copy, install by hand
prepare.py           applies config.yaml: renames, generated blocks, .env files (initial zip and patches)
config.example.yaml  every config.yaml key, documented; config.yaml itself is the owner's and git-ignored
check_wire.sh        checks the two wire.py copies are identical
tools/make_patch.py  builds dist/toolkit_cm-initial.zip and dist/patch_cm-vN.zip
docs/                architecture.md, decisions.md, open-items.md, worker-contract.md, metrics-draft.md
agent/               distribution "agent": the `agent` command
  README.md  .gitignore  .env.example  pyproject.toml  config.py  agent/  tests/  deploy/agent.service
cluster-manager/     distribution "cluster-manager": the `cm` command, including `cm daemon`
  README.md  .gitignore  .env.example  pyproject.toml  cluster_manager/  tests/  deploy/cluster-manager.service
file-server/         distribution "file-server": the `file-server` command
  README.md  .gitignore  .env.example  pyproject.toml  file_server/  deploy/file-server.service
```

- No project imports from another, and every module path inside a project is unchanged. The contracts between them are `wire.py`, kept as two identical copies (`agent/agent/wire.py` and `cluster-manager/cluster_manager/daemon/wire.py`), and the file server's HTTP API, which only `clients/file_server.py` in the daemon and the agent's `UpdateManager` know. `check_wire.sh` compares the two copies and fails, showing the diff, when they differ; run it before every release.
- Each service is one console script, run in the foreground from its virtualenv, and its systemd unit runs the same command: `agent` (`agent.__main__:main`), `cm daemon` (`cm` is `cluster_manager.client.cli:app`), and `file-server` (`file_server.file_server:main`). Each runs uvicorn with the app object, so each is one process.
- Every service reads its settings only from `.env` in its own project folder, with the same rules: the process environment is ignored, no defaults, unknown keys rejected. `.env.example` lists every key with example values and is committed; `.env` is ignored by git.
- Each project is installed by hand, following its README: a virtualenv in the project folder, `pip install -e .`, `.env` from `.env.example`, the command in the foreground, then the unit from `deploy/`. The owner sets up the virtualenvs and units on every machine himself, so the units in `deploy/` are a starting point, not a contract. The install is editable on purpose: each config finds its `.env` from its own source file's location, so the source must stay in the project folder rather than be copied into site-packages. There are no install scripts.
- Dependencies. `agent`: fastapi, uvicorn, httpx, pydantic, pydantic-settings, psutil; `config.py` is a top-level module. `cluster-manager`: fastapi, uvicorn, httpx, pydantic, pydantic-settings, `sqlalchemy[asyncio]` (async mode needs greenlet), asyncpg, apscheduler 3.x, typer and rich. `file-server`: fastapi, uvicorn, httpx, pydantic, pydantic-settings. pytest is a `dev` extra in the agent and the cluster manager. The cluster manager's `tests/` holds the canary's and the fleet's judgement tests, which need no `.env`.
- Each `.gitignore` covers Python caches, build output and virtualenvs, plus `.env`.
- The units expect each project at `/opt/<folder>`: `/opt/agent`, `/opt/cluster-manager` and `/opt/file-server` (`WorkingDirectory` and `ExecStart`).
- `deploy/agent.service`: `KillMode=process` (the worker outlives the agent), `TimeoutStopSec=40` (open connections 5s, the tick in progress up to the 5s heartbeat, and the ~20s operation grace), `PrivateTmp=no` (console logs are in `/tmp`), `Restart=always`.
- `deploy/cluster-manager.service` and `deploy/file-server.service`: one process each.
- First install order: the file server, then the cluster manager (its first boot fetches the bootstrap version through the file server), then the agents.
- Releases are git tags `v0`, `v1`, ... `tools/make_patch.py --initial` zips the newest tag as `toolkit_cm-initial.zip` (with a `VERSION` file, everything under `toolkit_cm/`); `tools/make_patch.py` tags HEAD as the next version and builds `patch_cm-vN.zip` (top folder `patch_cm-vN/`) from the version before. The owner's proprietary details live in one `config.yaml` that never enters the repository; every zip carries `prepare.py`, which applies it.
- `prepare.py` (needs pyyaml and pydantic; `--check` only validates) reads `config.yaml` next to it and, in order:
  1. validates everything it knows before changing anything (unknown keys are skipped with a warning, so a newer `config.yaml` works with an older release): the worker contract (as the old metrics generator did: unique fields and names, commands without a leading slash), every `{config.x}` in a command against the agent's settings, the inventory fields, and the renames;
  2. renames each placeholder in `rename:` (`hypertree`, `program_log`, `reload_phase`, `reload_phase1`, `reload_phase2`, `etc_dir`; longest first, so one name per phase can be given, with or without `reload_phase`) in every form (`program_log`, `PROGRAM_LOG`, `ProgramLog`, `program log`, `Program log`), inside text files and in file and folder names, skipping itself, the configs, `.git`, virtualenvs, caches and binaries. A new name must be snake_case and can't contain a placeholder, so running it again is a no-op; a folder already prepared can take new renames, but one whose earlier names change, or a new name overlapping an earlier one, is refused (`PREPARED` records them). Renaming `hypertree` changes the wire fields, the file server's routes and its stored file name together, so every service is prepared with the same `config.yaml`;
  3. rewrites every generated block (`# BEGIN generated: <name>` to `# END generated: <name>`): the worker contract, the inventory fields, and the inventory settings in `agent/config.py` and `agent/.env.example`. Names of ours in a block are renamed; values from `config.yaml` (commands, metric names) are written exactly as given;
  4. writes each service's `.env` (and `cluster-manager/deploy/postgres.env`) from its `.env.example` and `config.yaml`'s `env:` values, after the renames and the generated settings, so the template has every key this release needs. A section left out of `env:` writes no `.env`; a section given must have every key, and a missing key or one with no value (`KEY:` or `KEY: null`) stops `prepare.py` in step 1, before anything changes. `""` is a value and writes the key empty. Keys may be written in placeholder or renamed form; the files are chmod 600.
- A patch zip holds `prepare.py`, `README.txt`, `PATCH` and one folder per changed service (`root/` for the top level), each with `old/` and `new/` copies of its changed files, `DELETED.txt` when files go, `PATCH` and `apply.sh`. The agent's generated files and each included service's `.env.example` are always carried. `prepare.py` treats each `old/` and `new/` as a tree, and writes each service's `.env` at the top of its folder. `bash apply.sh [--force] TARGET` refuses an unprepared folder or a target without the service's marker file, compares every target file with `old/` outside the generated blocks and changes nothing if one differs (unless `--force`), backs up to `TARGET/.patches/vN/`, copies `new/` in, deletes `DELETED.txt`'s files, logs to `TARGET/.patches/applied` (a second run says so and stops), and lists `.env` keys the new `.env.example` adds or drops. It never touches a `.env`.
- The patch's top-level `apply.sh [--force] INSTALL_FOLDER` does it all on a prepared install folder (the owner keeps it in git): it copies that folder's current `config.yaml` in and prepares the patch on every run, so `config.yaml` edits are picked up, runs every service folder's `apply.sh --check` first and changes nothing if any would be refused, then applies each (`root/` to the folder itself, a missing service folder skipped). `.patches/` is git-ignored, so `git status` afterwards shows only the patch.

## Cluster manager daemon and client

The cluster manager is a daemon (`cluster_manager/daemon`). People drive it through a client (`cluster_manager/client`, the CLI `cm`). Both live in the `cluster-manager` project. The two talk over HTTP and agree on shared request and response schemas (`cluster_manager/schemas.py`), the same way `wire.py` is the contract between the agent and the cluster manager. It is a daemon on purpose, so a frontend or other callers can send the same requests later. The client never talks to agents, and agents only talk to the daemon through the heartbeat.

Upstream and the file server call a version a **job**. The job id is always exactly the version string the worker's version endpoint returns (upstream often uses the form `job_<n>`), so there is one identifier. Code facing upstream and the file server (`clients/file_server.py` and the file server itself) says `job_id`; the wire, the database, the services and the CLI say `version`.

Version dispatch is the daemon's main client request, served by the `version_dispatch` endpoint (`POST /versions/dispatch`, also served at the alias `POST /versions/signature_dispatch`; `services/versions.dispatch`):
1. The request names the new version (the job) and the upstream endpoint it is published at. A version already in `version_deployments` is refused (a version is never deployed twice), and so is a dispatch while another version is VERIFYING. The endpoint is passed to the file server as given, and it alone decides which build of the job is fetched (for example the optimized one).
2. The daemon asks the internal file server to fetch the job (`FileServerClient.fetch`, `POST /jobs/<job_id>` with the upstream endpoint) and waits for the answer. The file server streams the artifact from upstream, stores it, and answers with its sha256, which becomes `artifact_sha256`. The fetch is idempotent: a job already stored is returned as it is, and one whose artifact is gone is fetched again. A failed fetch is a 502 from the daemon and writes nothing.
3. The daemon then starts the canary (see "Canary"): in one transaction it picks the canary agents, the version enters VERIFYING with the file server's URL and that sha256, and the canary agents' desired state points at it with a bumped `generation`. If no agent is eligible, dispatch returns an error and nothing enters VERIFYING. After the commit it schedules the canary judgement.

Every `artifact_url` in a `DesiredState` points at the internal file server. Agents download only from there, never from the upstream endpoint and never from the cluster manager, which serves no artifacts: it only says which file-server URL to fetch and which hash to expect. The agent's `download(artifact_url, artifact_sha256, version)` takes whatever it is given.

File-server URLs are built in one place in the daemon, `clients/file_server.py`, and nowhere else: agents and the rest of the daemon never derive one URL from another. The file server stores each artifact under its job id alone. Its stored copy is neither "optimized" nor "unoptimized": which build was fetched was decided by the upstream endpoint, and nothing in the daemon or the agent needs to know. A job's artifact URL is `<file server>/jobs/<job_id>/artifact`, and its hypertree URL is `<file server>/jobs/<job_id>/hypertree`. The heartbeat reply puts the hypertree URL of the desired version in both `hypertree_get_url` and `hypertree_upload_url`. `version_deployments` stores `artifact_url` per version and nothing about hypertrees; the hypertree URL is built from the version when the reply is built.

Hypertrees: loading a version makes the worker generate a hypertree, a multi-tier folder named by a hash. What the file server stores for a job is a zip of that folder, with the folder as its top entry, so whoever unzips it gets the hash name intact; neither the daemon nor the file server tracks the hash. A GET on the job's hypertree URL returns it, or 404 when there is none yet; a PUT stores it, with 409 when the job already has one. Agents clear, fetch and upload hypertrees during UPDATE, as non-critical steps; nothing about a hypertree can fail or block an update.

### File server

Code: `file_server/file_server.py` in the `file-server` project, a single-file FastAPI app. Only the daemon and the agents call it. It runs under uvicorn as one process (its per-job locks live in memory), on the internal network, with no authentication, like the rest of the system.

Storage, one directory per job under `FILE_SERVER_ROOT`:

```
<root>/<job_id>/artifact    the artifact (a zip, checked before it is stored), exactly as fetched
<root>/<job_id>/job.json    JobInfo; written last, after the artifact
<root>/<job_id>/hypertree   a zip of the worker's <hash>/ folder, uploaded by the first agent that loads the job
```

| Method and path | Caller | Does |
|---|---|---|
| `POST /jobs/{job_id}` `{"upstream_url", "expected_sha256"?}` | daemon | Streams the artifact from upstream, hashing as it writes, checks it is a valid zip, and answers `JobInfo(job_id, sha256, size, upstream_url, fetched_at)`. Idempotent: a stored job is returned as it is; one whose artifact is missing or not `job.json`'s size is fetched again. An upstream error or an artifact that isn't a valid zip is a 502 and leaves nothing stored. With `expected_sha256`, an artifact with another hash (stored or just fetched) is a 409 and a fetched one isn't stored; the client raises `ArtifactMismatch`. |
| `GET /jobs/{job_id}` | anyone | The stored `JobInfo`, or 404. |
| `GET /jobs/{job_id}/artifact` | agents | The artifact, or 404 unless the job is stored (`job.json` there, and the artifact with its size). |
| `GET /jobs/{job_id}/hypertree` | agents | The hypertree, or 404 when there is none yet. |
| `PUT /jobs/{job_id}/hypertree` (raw body) | agents | Stores it (201). 409 when the job already has one, 404 for an unknown job. |

Rules:
- A job id must match `^[A-Za-z0-9._-]{1,128}$` (and isn't `.` or `..`), or the request is refused with 422, since it becomes a directory name. The agent's `download` applies the same rule to the bundle folder name.
- Artifacts are zips, since agents unzip them into bundles. Before an artifact is stored, the server checks that it opens as a zip and that every member passes its CRC check (`zipfile.is_zipfile` and `ZipFile.testzip()`), so a truncated or non-zip download from upstream never becomes a job. Hypertrees aren't checked.
- Every file is written to a `.tmp` name, fsynced, then renamed into place, and `job.json` is written last. A crash part-way leaves no job that anyone is served, and the next fetch starts it again. A job counts as stored only while `job.json` and an artifact of its size are both there, so one deleted by hand, whole or only its artifact, is fetched again by the next `POST`.
- A fetch and a hypertree upload hold the job's lock, so two concurrent fetches of one job fetch once, and only one upload wins.
- Nothing is ever deleted: the owner keeps every job, since storage is cheap.
- Settings come only from `.env` in the `file-server` project folder (`FileServerConfig`), with the same rules as the other services: the process environment is ignored, no defaults, no unknown keys. `FILE_SERVER_ROOT`, `FILE_SERVER_UPSTREAM_TIMEOUT` (seconds) and `FILE_SERVER_PORT`.
- `main()` is the `file-server` command. It runs uvicorn with the app object on `0.0.0.0` and `FILE_SERVER_PORT`, so there is always exactly one process.
- File writes are synchronous inside async handlers, so they block the event loop for each chunk; with two callers that is accepted.

The daemon's client, `FileServerClient(base_url, *, timeout, fetch_timeout, transport)` in `clients/file_server.py`, has `fetch(upstream_url, job_id) -> JobInfo` (under its own, longer `fetch_timeout`), `artifact_url(job_id)`, `hypertree_url(job_id)` and `aclose()`. Its `JobInfo` is its own copy of the server's model, with `sha256` checked against a 64-hex-digit pattern where it enters. An error status raises `FileServerError` with the status and the first 200 characters of the body; a network failure or timeout propagates as the `httpx` exception. The daemon builds one client from `config.file_server_url` in its startup and closes it at shutdown.

### Daemon configuration and wiring

Code: `cluster_manager/daemon/config.py` (`DaemonConfig`, `config`), `db/session.py`, `app.py`, `__main__.py`.

- Settings come only from `.env` in the `cluster-manager` project folder, with the same rules as the agent's `config.py`: the process environment is ignored, no defaults, no unknown keys. Durations are plain seconds (`non_responding_seconds`, `canary_duration_seconds`), exposed as `timedelta` properties (`non_responding_after`, `canary_duration`).
- `db/session.py` builds the async engine (`pool_pre_ping=True`) and `session_factory` (`expire_on_commit=False`). `get_session` is the FastAPI dependency.
- The schema is created at startup with `Base.metadata.create_all`, which creates missing tables. While the data can still be wiped there are no migrations: a changed model means dropping the database file.
- The lifespan, in order: create the schema, build the file-server and Mattermost clients and the APScheduler `AsyncIOScheduler` (UTC), run `ensure_bootstrap`, reschedule a pending canary judgement, start the scheduler, start the watcher, the sender, the fleet watch and the file watch as explicit tasks (a crash is logged). Shutdown cancels the loops, stops the scheduler, closes the clients and disposes of the engine. The file-server client and the scheduler are on `app.state` for the routes.
- `__main__.main()` configures logging from `config`, then runs uvicorn with the app object on `0.0.0.0` and `config.port`, one process (the scheduler and the loops live in it). `cm daemon` calls it, and the systemd unit runs `cm daemon`.

### The CLI (cm)

Code: `cluster_manager/client/cli.py` (Typer and Rich), `client/daemon_client.py`.

- `cm` is a Typer app. Each command is a function whose docstring is its help text, and `cm --help` lists them all. Global options come before the command: `--daemon-url` (or `CM_DAEMON_URL`; default `http://127.0.0.1:<PORT>` with `PORT` from the project's `.env`, or port 8000 without one) and `--json`.
- Every command except `cm daemon` sends one request through `DaemonClient` and prints the response with Rich, or as JSON with `--json`. The daemon decides everything (conditions, canary outcomes); the CLI only renders. A daemon error exits 1, and an unreachable daemon exits 2.
- `cm daemon` runs the daemon in the foreground. It imports the daemon only inside the command, since the daemon's config reads `.env` at import: every other command works on a machine with no `.env`.
- Rich renders lists as tables with a line under the headers and no borders, and the agent detail, status and canary views as aligned grids. Only `condition`, the canary `outcome` and a step's state are coloured; a step with `attempts == 0` and `ok` shows as `skipped`. Daemon text is printed with markup off, so it is never read as Rich markup. On a narrow terminal a cell wraps rather than being cut short. When output is piped, rows stay on one line so `grep` works.
- `--condition` takes the `AgentCondition` enum, so `--help` lists the valid values and anything else is rejected before a request is sent.
- Tracebacks don't show local variables (`pretty_exceptions_show_locals=False`), so a `cm daemon` crash can't print configuration values.

### Bootstrap

The first ACTIVE version comes from config. `services/versions.ensure_bootstrap(session, file_server, config.bootstrap_version, config.bootstrap_upstream_url, now)` runs once in the daemon's startup, after the schema exists and before anything reads the ACTIVE version (the canary job's reschedule, the API, the heartbeat):
- First boot, when `version_deployments` is empty (`VersionDeploymentRepo.has_any()` is false): it calls `file_server.fetch(bootstrap_upstream_url, bootstrap_version)`, then inserts the version as ACTIVE with `upstream_url` from the config, `artifact_url` from the client, `artifact_sha256` from the fetch's answer, `created_at` and `verified_at` set to `now`, `activated_by_rollback=false` and no `unstable_reason`, and commits. The fetch is idempotent, so an artifact already on the file server isn't fetched again. If the fetch fails, startup fails.
- Every later boot: the config values aren't used. Startup only checks that exactly one version is ACTIVE (`VersionDeploymentRepo.active()` raises otherwise), and fails if not. Changing the bootstrap settings after first boot does nothing; the ACTIVE only changes through promotion and rollback.
- The seed is a verified version (`verified_at` set): when the first canary is promoted it becomes STABLE, and `latest_stable` returns it as the rollback target.

### Heartbeat service

Code: `api/heartbeat.py` (`POST /heartbeat`, `HeartbeatReport` in, the bare `DesiredState` out), `services/heartbeat.py` (`handle`). One transaction, in this order:
1. `str(report.ipv4)` once, where the IP reaches the database.
2. An unknown IP registers: the agent row, its desired state seeded from the report (`generation = acknowledged_generation + 1`, `restart_generation = handled_restart_generation`, `force_update_generation = handled_force_update_generation`, `desired_running=true`, the ACTIVE version) and its history row, flushed before anything refers to them. A decommissioned IP is refused (403).
3. The observed row is merged from the report, with `last_heartbeat_at` from the daemon's clock and the operation views as JSON.
4. A `last_operation` is recorded (idempotent per `op_id`).
5. An open `non_responding` incident is closed. `metrics_available=False` opens a `metrics_unavailable` incident (idempotent), `True` closes it, and `None` does neither.
6. The reply is built from the desired state and its version row alone, with the desired version's hypertree URL in both hypertree fields, then the transaction commits.

### Canary

Selection, at dispatch:
- The canary is a random sample of eligible agents: live (not decommissioned, last heartbeat within the non-responding threshold) and last reported `process_status == running`. Agents in other states wouldn't run the UPDATE straight away: a STOPPED worker with `desired_running=false` never would, and a CRASHED one does a CRASH_RESTART on the old bundle first.
- The size is fixed, `config.canary_size`. With fewer eligible agents it shrinks to however many there are, down to one. With none, dispatch returns an error.
- Selection runs in the transaction that inserts the VERIFYING row, so the error leaves no VERIFYING version.
- The canary agents aren't stored: they are the agents whose `desired_state.version` is the VERIFYING version.

Judging, at the deadline (`services/versions.judge_canary`):
- The canary is judged once, at `created_at + config.canary_duration`, by an APScheduler date job (`canary-<version>`) scheduled at dispatch, with `misfire_grace_time=None` so a deadline that passed while the daemon was down runs as soon as the scheduler starts. At startup the daemon reschedules it when a VERIFYING version exists, so a daemon restart doesn't lose it. There is no early exit. A job that finds the version no longer VERIFYING does nothing.
- The job loads the live canary agents' desired and observed rows in one join, and a pure function (`services/conditions.canary_outcome`) gives each agent one outcome, checked in this order:
  1. FAULTY_UPDATE: `failed_generation == generation` (the agent tried the canary change and gave up).
  2. CRASHED: `process_status == crashed`.
  3. NOT_CONVERGED: not RUNNING, or `running_version` isn't the VERIFYING version.
  4. NO_VERDICT: `violations is None`.
  5. PERFORMANCE: non-empty `violations`.
  6. PASSED.
- Only the state at the deadline counts. A worker that crashed earlier and is RUNNING the target version at the deadline is fine. An agent whose metrics are unavailable at the deadline is NO_VERDICT, which counts in no bucket (the owner's choice).
- Outcomes are counted in buckets, each with its own limit, all counts from `config`: crash (FAULTY_UPDATE and CRASHED) against `canary_max_crashes`, the lowest; PERFORMANCE against `canary_max_violations`; NOT_CONVERGED against `canary_max_not_converged`. NO_VERDICT and PASSED count in no bucket. The limits are fixed thresholds; the canary isn't compared with the ACTIVE fleet.
- A canary where no agent converged is always rejected, whatever the limits: converged means NO_VERDICT, PERFORMANCE or PASSED (running the canary version). That includes a canary whose agents were all decommissioned before the deadline: with no outcomes, passing it would promote the version on no evidence. The example settings put `canary_max_not_converged` at 0, so any canary agent not running the new version at the deadline rejects it.
- Any bucket over its limit rejects the canary: VERIFYING → UNSTABLE with `unstable_reason` holding the count per outcome, and the canary agents go back to the ACTIVE version.
- Otherwise the version is promoted automatically: the old ACTIVE → STABLE (flushed first), VERIFYING → ACTIVE with `verified_at` set and `activated_by_rollback=false`, and every live agent not already on the new version points at it, in one transaction.
- A converged agent is NO_VERDICT until its performance monitor has a full window (5 minutes of RUNNING with no operation), so `canary_duration` must be comfortably longer than the update plus one window, or every canary agent is NO_VERDICT.

Queries: no per-agent queries. Picking the sample is one query, `ObservedStateRepo.canary_sample(size, now, threshold)` (filter, `ORDER BY random()`, `LIMIT`). Pointing a set of agents at a version (canary start, reject, promote, rollback) is one `UPDATE ... RETURNING` over the list of IPs, `DesiredStateRepo.point_at_version(ips, version, now)`, which bumps each `generation` in the database like `apply`. The service then builds the history rows from the returned rows and adds them with `add_history_many`, which flushes as one batched insert.

### Rollback

`services/versions.rollback(session, *, reason, expected_active)` is the one rollback, triggered two ways: a human requests it (`cm rollback`, `POST /versions/rollback`, reason "rolled back by request"), or the fleet watch fires it (see "Fleet watch"). It is refused when the ACTIVE became ACTIVE by a rollback (rollbacks never chain), when there is no STABLE version, and, when `expected_active` is given, when that version is no longer the ACTIVE one, so a verdict about one version never rolls back another. Otherwise, in one transaction: ACTIVE → UNSTABLE with `unstable_reason` "<reason> at <time>" (flushed first), the latest STABLE → ACTIVE with `activated_by_rollback=true`, and every live agent whose desired version was the old ACTIVE points at the new one. Canary agents running a VERIFYING version stay on it.

### Fleet watch

Code: `loops/fleet_watch.py`, `services/versions.fleet_verdict`, `services/conditions.fleet_outcome` and `fleet_failing`.

After promotion, the ACTIVE fleet stays under judgement for as long as the version is ACTIVE, and a failing fleet is rolled back automatically:
- Every tick (10s), `fleet_verdict` judges the live agents whose desired version is the ACTIVE one, each by the canary's rules (`canary_outcome`), and counts them in the canary's buckets: crash (FAULTY_UPDATE and CRASHED), PERFORMANCE and NOT_CONVERGED. The limits are the fleet's own, higher counts: `fleet_max_crashes`, `fleet_max_violations` and `fleet_max_not_converged`. NO_VERDICT and PASSED count in no bucket.
- An agent counts only once it has settled: it has acknowledged its current desired state and no operation is running. Non-responding agents and agents with `desired_running=false` don't count either. Without this, every promotion would fail at once, since the whole fleet is not converged until its UPDATEs finish. Unlike the canary, a fleet with nothing to count isn't failing.
- A breach must last `fleet_breach_seconds` (`0` = the first over-limit reading) before the rollback fires. A crash is followed within a tick by the agent's own CRASH_RESTART, so a single reading isn't enough. The breach lives in the loop's memory: it starts again when the ACTIVE version changes, when the fleet comes back within its limits, and after a daemon restart, which can only delay a rollback.
- Only an ACTIVE that can be rolled back is watched: one that came from a rollback, or one with no STABLE behind it (the bootstrap seed before the first promotion), is skipped. The rollback passes the judged version as `expected_active`.
- The rollback's `unstable_reason` records the counts ("rolled back automatically: fleet over its limits (crashed 1, no_verdict 2) at <time>"), and the loop logs the breach, its end and the rollback.

### File watch

Code: `loops/file_watch.py`.
- Every 60s (`FILE_WATCH_INTERVAL`) it reads the versions that aren't UNSTABLE, closes the session, and calls `file_server.fetch(upstream_url, version, expected_sha256=artifact_sha256)` for each. A stored version costs one request; one deleted by hand on the file server, whole or only its artifact, is fetched again from upstream.
- `ArtifactMismatch` (upstream now serves another hash; nothing is stored) opens an `ARTIFACT_MISMATCH` incident with `source=CLUSTER_MANAGER`, `ip` the daemon's `IPV4` and the version. The next fetch that succeeds closes it. Any other failure is logged and tried again on the next tick.

### Incidents: watcher and sender

Code: `loops/watcher.py`, `loops/sender.py`, `clients/mattermost.py`.
- The watcher runs every tick (10s). It opens a `non_responding` incident for each live agent whose `last_heartbeat_at` is older than `non_responding_after` (idempotent: one open incident per agent and kind), and commits. The agent's next heartbeat closes it.
- The sender runs every 10s. It reads the pending openings and recoveries, ends that read, then posts each one to the Mattermost incoming webhook (`{"text": ...}`) outside any transaction, and marks it sent in its own small transaction only after the post succeeds. An outage delays messages without losing them, and logs only its first failure and the recovery. An opening always goes out before its recovery. With an empty `mattermost_webhook_url`, nothing is sent and incidents stay pending.

### Target layout

Inside the `cluster-manager` project:

```
cluster_manager/
  schemas.py            the client↔daemon contract
  daemon/
    __main__.py         main(): what `cm daemon` runs
    app.py
    config.py
    wire.py
    api/
      heartbeat.py
      fleet.py
      agents.py
      versions.py
    services/
      heartbeat.py
      desired_state.py
      versions.py
      views.py
      conditions.py
      errors.py
    loops/
      watcher.py
      sender.py
      fleet_watch.py
      file_watch.py
    clients/
      file_server.py
      mattermost.py
    db/
      base.py
      session.py
      models/
        agent.py
        desired_state.py
        observed_state.py
        operation.py
        version_deployment.py
        incident.py
      repositories/
        agent_repo.py
        desired_state_repo.py
        observed_state_repo.py
        operation_repo.py
        version_deployment_repo.py
        incident_repo.py
  client/
    cli.py              the cm Typer app, rendered with Rich
    daemon_client.py
```

- The internal file server (see "File server") is its own project, `file-server`, and imports nothing from the cluster manager or the agent.
- `wire.py` is the daemon's copy of `agent/wire.py`, identical to it.
- `api/heartbeat.py` takes a `HeartbeatReport` and returns the bare `DesiredState`. `api/fleet.py` serves the client's reads, `api/agents.py` its per-agent actions, and `api/versions.py` dispatch (`version_dispatch`, `POST /versions/dispatch`, with the alias `POST /versions/signature_dispatch`) and rollback.
- `services/` holds the behaviour and owns the transactions. `services/heartbeat.py` is described under "Heartbeat service". `services/desired_state.py` holds the per-agent mutators: every change bumps `generation` and writes a history row. `services/versions.py` holds the bootstrap, dispatch, canary judging, the fleet verdict and rollback, keeping exactly one ACTIVE in each transaction. `services/views.py` builds the client's read views and `services/conditions.py` the pure judgements they and the canary job share.
- Repositories never commit; the service calling them does.
- `clients/` holds the outbound HTTP clients: the internal file server and the Mattermost webhook. `clients/file_server.py` is the daemon's only code that knows the file server's API (see "File server"). Agents call the file server themselves for downloads and hypertrees, with the URLs the daemon gave them.
- `loops/fleet_watch.py` judges the ACTIVE fleet every tick and rolls back automatically (see "Fleet watch").
- `loops/file_watch.py` keeps every version that isn't UNSTABLE on the file server (see "File watch").
- `cluster_manager/schemas.py` is the client↔daemon contract. It defines the status enums the client shows and the database stores, `DeploymentStatus` and `IncidentKind`, and the database models import them from there, the same way they import `ProcessStatus` and `OpType` from `wire.py`. One definition serves both, so the client's `VersionRow.status` and `IncidentRow.kind` are typed rather than plain strings.

### Database (daemon/db)

Code: `cluster_manager/daemon/db/base.py` (`Base`, `UtcDateTime`), `db/session.py`, `cluster_manager/daemon/db/models/*.py` and `cluster_manager/daemon/db/repositories/*_repo.py`. SQLAlchemy 2.0 typed models with an async session, on PostgreSQL through asyncpg. The models were first written for Postgres, ported to SQLite on 2026-11-04, and moved back to PostgreSQL on 2026-11-05. The database runs in a podman container from the Bitnami PostgreSQL image (`deploy/cluster-manager-postgres.container`, a Quadlet unit), listening on loopback, its data in the volume `cluster-manager-pgdata`; the daemon's unit starts after it.

Rules for every model:
- All database access goes through SQLAlchemy, and there is no literal SQL. Every `Index` and `CheckConstraint` is built from the model's attributes (`Operation.agent_ip`, `VersionDeployment.status == DeploymentStatus.ACTIVE`), never from column names as strings, and is declared after the class at module level. Built from columns of one table, each attaches to that table by itself, so no model has `__table_args__`. A misspelled column or enum member fails at import, and a type checker sees it.
- The column type comes from the `Mapped[...]` annotation, and so does nullability (`X | None` is nullable). A column only declares a type when the annotation alone gets it wrong: timestamps are `UtcDateTime()`, and JSON columns are declared as the generic `JSON`.
- `UtcDateTime` (in `db/base.py`) is the type of every timestamp column: a `timestamptz` that reads back in UTC, which satisfies the `AwareDatetime` fields in `wire.py` and the client schemas. It rejects a naive value on write.
- Every timestamp comes from the daemon's clock (`datetime.now(UTC)`), passed into the repositories as `now`. No query uses `func.now()` or date arithmetic in SQL, so every timestamp comes from one clock. Cutoffs are computed in Python (`now - threshold`) and compared as bound parameters, which go through `UtcDateTime` too.
- JSON columns that can be null use `JSON(none_as_null=True)`, so Python `None` is SQL `NULL` and `[]` is `'[]'`: the two stay distinct.
- Inserts that need `ON CONFLICT DO NOTHING` use `sqlalchemy.dialects.postgresql.insert`.
- Enums are plain `Mapped[SomeStrEnum]`, so the database stores the member **name** (`ACTIVE`, `CRASHED`). Code always assigns and compares members, and SQLAlchemy converts both ways, including when it renders an index condition or a check constraint.
- Counters (`generation` and the per-agent counters) are plain `INTEGER`.
- Agent IPs are plain strings in every table. The heartbeat service converts the wire's `IPv4Address` with `str(report.ipv4)` once, where it reaches the database, so every key is in canonical form.
- Partial unique indexes declare their condition with `postgresql_where`. Without it, the index would be a plain unique index, allowing a single row per status or per agent and kind.
- No column defaults. The service builds every row with every value, as for the agent's persisted models. The only database-generated value is `incidents.id`, an identity key.
- Transactions are `READ COMMITTED`, PostgreSQL's default. A write to a row that another transaction has changed waits for that one to commit, so two transactions never change the same rows at once, and the unique indexes hold the invariants. This is what SQLite gave too: its driver begins a transaction at the first write, so a service's earlier reads were never isolated either. `SERIALIZABLE` was tried and rejected: concurrent heartbeats from different agents fail it as false-positive conflicts.

Per model:
- `version_deployment`: `VersionDeployment(version, upstream_url, artifact_url, artifact_sha256, status, created_at, verified_at, activated_by_rollback, unstable_reason)`, with `status` a `DeploymentStatus` (defined in `cluster_manager/schemas.py`). Two partial unique indexes enforce at most one VERIFYING and at most one ACTIVE. Two check constraints enforce that ACTIVE and STABLE have `verified_at` set and VERIFYING doesn't; UNSTABLE may have either. A third enforces that `unstable_reason` is set exactly when the status is UNSTABLE. A unique index is checked per statement, not at commit, so a transition that moves ACTIVE from one row to another must demote the old row, `flush`, then promote the new one. `VersionDeploymentRepo` has `get`, `has_any` (whether any version exists, for the bootstrap), `active` (raises if there isn't exactly one), `verifying`, `latest_stable` (the rollback target, by `verified_at`), `all`, `not_unstable` (for the file watch) and `add`.
- `agent`: `Agent(ip, registered_at, decommissioned_at)`, keyed by the IP. `decommissioned_at` is null while live; the heartbeat service refuses an agent once it's set. `AgentRepo` has `get`, `add`, `list_live` (with its desired and observed rows, optionally for one version) and `get_with_state`.
- `desired_state`: `DesiredStateRow(ip, generation, restart_generation, force_update_generation, desired_running, version, updated_at)`. It is named apart from the wire `DesiredState`, which the reply is built into. `version` is a foreign key to `version_deployments`; `artifact_url` and `artifact_sha256` are read from there, not copied, and `hypertree_get_url` and `hypertree_upload_url` are both the file-server client's `hypertree_url(version)`. `DesiredStateHistoryRow` has the same columns with `changed_at` instead of `updated_at`, keyed by `(ip, generation)`, so a change that doesn't bump the generation fails on the key.
  - `DesiredStateRepo.apply(ip, now, *, desired_running, version, bump_restart, bump_force_update)` is the only way to change one existing row. It is one `UPDATE ... RETURNING` that always sets `generation = generation + 1` and `updated_at`, and changes only the fields given. Counter bumps are also computed in the database (`restart_generation + 1`), so the caller never reads a counter first. It returns the new row; the service writes the history row from it. Changes to many agents at once use the multi-agent update described under "Canary".
  - `get_with_deployment` returns the row and its `VersionDeployment` for the heartbeat reply. `add` is for registration, and `add_history` for every change.
- `observed_state`: `ObservedState`, one row per agent with one column per `HeartbeatReport` field, plus `last_heartbeat_at` from the daemon's clock. `violations` is `JSON(none_as_null=True)`, so `NULL` and `[]` stay distinct. `current_operation` and `last_operation` are `JSON(none_as_null=True)`, converted explicitly in the heartbeat service with `OperationView` (`model_dump(mode="json")` on write, `model_validate` on read). `ObservedStateRepo.save` merges the whole row on every heartbeat. `silent_ips(now, threshold)` returns the live (not decommissioned) agents whose `last_heartbeat_at` is before `now - threshold`, for the watcher.
- `operation`: `Operation(agent_ip, op_id, generation, type, started_at, finished_at, failure, steps)`, one row per finished operation, keyed by `(agent_ip, op_id)`, with an index on `(agent_ip, generation)`. `steps` is `JSON`, read back with `StepView`. Success is `failure IS NULL`. `OperationRepo.record` inserts with `ON CONFLICT DO NOTHING`, so the heartbeat service can call it on every report that carries a `last_operation` and only the first one writes. `for_generation` and `recent` read it back.
- `incident`: `Incident(id, source, ip, version, kind, opened_at, closed_at, notified_at, recovery_notified_at)`, with `source` an `IncidentSource` and `kind` an `IncidentKind` (both defined in `cluster_manager/schemas.py`). `source` is `AGENT` for an incident about an agent, with `ip` the agent's, and `CLUSTER_MANAGER` for one the daemon raises about itself, with `ip` the daemon's `IPV4` and `version` the version it is about. An incident is information only: no foreign keys. The kinds: `NON_RESPONDING`, opened by the watcher from silence, `METRICS_UNAVAILABLE`, opened by the heartbeat service from the report, and `ARTIFACT_MISMATCH`, opened by the file watch. A partial unique index on `(source, ip, version, kind) WHERE closed_at IS NULL`, with `NULLS NOT DISTINCT`, allows one open incident per source, IP, version and kind. A check constraint allows `recovery_notified_at` only once the incident is closed and its opening was sent. Every timestamp is the `now` the caller passes in.
  - `IncidentRepo.open(ip, kind, now, *, source, version=None)` inserts with `ON CONFLICT DO NOTHING` on the partial index and returns `None` when one is already open, so the watcher can call it every tick for every silent agent. `close_open(ip, kind, now, *, source, version=None)` closes the open one, from the heartbeat. `open_for_agent(ip)` returns an agent's open `AGENT` incidents.
  - The sender reads `pending_openings` (`notified_at` null) and `pending_recoveries` (closed, opening sent, `recovery_notified_at` null), and calls `mark_opening_sent(id, now)` or `mark_recovery_sent(id, now)` only after the post succeeds. Each message is therefore sent once, and an opening always goes out before its recovery. `open_incidents` is for the dashboard.

`generation` identifies a change to one agent's desired state, not a state and not an operation. Every change bumps it once, however many fields change and however many operations the agent then runs. The same content can come back under a later generation. Numbers on different agents are unrelated. History answers what generation N asked for, and `operations` answers what the agent tried for it and where it failed.

## Rules for the orchestrator (cluster manager side)

- An agent is identified by its IPv4 address, which is unique and static and arrives in `HeartbeatReport`. The cluster manager keys the agent on it directly: there is no separate agent id, and no port is recorded. The first heartbeat from an unknown IP registers it: create the agent, its desired state and history in the heartbeat's transaction, seeded from the report (`generation = acknowledged_generation + 1`, `restart_generation = handled_restart_generation`, `force_update_generation = handled_force_update_generation`), and reply with the desired state. A new agent starts on the ACTIVE version with `desired_running=true`. Refuse heartbeats from decommissioned IPs. No hostname is reported or kept.
- Versions live in `version_deployments`, keyed by `version`, which must be exactly the string the worker's version endpoint returns (the agent runs UPDATE whenever `running_version` differs from `DesiredState.version`). It is also the file server's job id. `desired_state.version` references it. Its `artifact_url` is the internal file server's URL for the artifact, never the upstream one, and its `artifact_sha256` is the hash the file server reported. Statuses: VERIFYING (being canaried, at most one), ACTIVE (what the fleet runs and new agents get, exactly one), STABLE (passed verification, no longer ACTIVE; a rollback target), UNSTABLE (failed verification or was rolled back; never deployed again; `unstable_reason` says why).
- Transitions: new → VERIFYING (canary starts, only after version dispatch has put the artifact on the internal file server and at least one agent is eligible); VERIFYING → ACTIVE (passed at the canary deadline, automatically: `verified_at` set, old ACTIVE → STABLE, fleet follows); VERIFYING → UNSTABLE (failed at the canary deadline: canary agents go back to ACTIVE); rollback: ACTIVE → UNSTABLE and the latest STABLE by `verified_at` → ACTIVE, fleet follows. Leaving ACTIVE always happens in the same transaction that makes another version ACTIVE, so exactly one is ACTIVE at all times after startup. The first ACTIVE is the bootstrap version from config, seeded on first boot (see "Bootstrap"); startup fails without exactly one ACTIVE, and code that relies on the ACTIVE version, such as registration, doesn't check for it.
- Rollbacks never chain. A promoted ACTIVE stays under judgement for as long as it is ACTIVE: the canary's rules with higher limits, and a breach that lasts `fleet_breach_seconds` rolls back to the latest STABLE automatically (see "Fleet watch"). A human can also request a rollback. A version that became ACTIVE by a rollback (`activated_by_rollback=true`) is never rolled back: it stays until a new version passes verification and is promoted. With no STABLE version there is nothing to roll back to. `activated_by_rollback` is set every time a version becomes ACTIVE: false on promotion (and for the bootstrap seed), true on rollback. Rejecting a canary (VERIFYING → UNSTABLE) is not a rollback: it never touches the ACTIVE, so it is always allowed, including while the ACTIVE came from a rollback.
- Think of it as two fleets. The verifying fleet (the canary agents) runs the VERIFYING version, and going back means returning to the ACTIVE version, as often as needed. The main fleet runs the ACTIVE version, and going back means a rollback to the latest STABLE, which `activated_by_rollback` limits to once in a row.
- There is always an ACTIVE, also while a version is VERIFYING: the canary agents run the VERIFYING version and every other agent, including newly registered ones, runs the ACTIVE. The canary is judged against fixed limits (see "Canary"), not against the ACTIVE.
- Reply to every heartbeat with the bare `DesiredState`: all nine fields, flat, with no envelope and no "no change" reply. The agent keeps no copy, so a reply without the desired state leaves it unable to act, including on a crashed worker.
- `agent_status=crashed_hold` is the signal that matters in `agent_status`: the agent has used its one crash restart and needs a force bump or a human. `busy` is the same as `current_operation` not being null.
- `failed_generation == generation` means the agent tried the current desired state and gave up. It won't try again until `generation` changes.
- Don't time running operations. The agent bounds its own steps and reports a timeout as a normal failure.
- Bump `generation` with every change, including force bumps.
- `restart_generation` and `force_update_generation` are per agent and start at 0. Reset both to 0 when an agent is re-provisioned, since a re-provisioned agent starts its handled counters at 0 and would otherwise act once on the old values.
- `process_status == CRASHED` means the worker failed: its process is gone, or it's alive but hung (failed `max_failures` liveness checks in a row). The agent restarts it once on its own (CRASH_RESTART), then holds in CRASHED_HOLD. That budget resets after a successful UPDATE or FORCE_UPDATE, so a newly downloaded version always gets its own crash restart.
- A START, CRASH_RESTART or RESTART succeeds once the worker logs that it is ready; it doesn't check the version. If it never logs ready, the agent kills it and reports `failed_generation` and `process_status=crashed`, then tries one CRASH_RESTART if budget is left, so a worker that can't start ends in `crashed_hold`. A worker that comes up but stops answering its version endpoint shows up afterwards as `crashed`.
- A worker that died while the agent was down is reported as `stopped`, not `crashed`, and the agent starts it again (START) if `desired_running`. That includes a worker in CRASHED_HOLD when the agent restarts; the START doesn't reset its crash budget, so another crash goes straight to `crashed_hold`. To keep a worker down after stopping it by hand, set `desired_running=false`.
- Bump the force counter only when `process_status == CRASHED`. A bump while RUNNING stays pending until the next crash.
- Roll back a running worker with a normal UPDATE, and a crashed, hung or stopped one with a force bump.
- A failed `clear_hypertree`, `fetch_hypertree` or `upload_hypertree` step in an UPDATE's or FORCE_UPDATE's step log is not a failed update: those steps are non-critical, and the update's outcome is `failure` alone.
- `healthy` and `violations` are separate signals. A health dip while BUSY is expected and isn't counted. `violations=None` means no verdict yet: for the first window after an update, a restart or any operation, and while metrics are unavailable. Only whether `violations` is `None`, empty or non-empty matters; its messages are for humans and are never parsed.
- `healthy=False` means `max_failures` failed checks in a row, including a worker that stopped answering, and the same report then shows the worker as CRASHED. `healthy=None` means no data yet, including while an operation runs and right after an agent restart. A `restart_generation` bump doesn't apply to a hung worker, since RESTART only runs from RUNNING.
- Only the cluster manager alerts. The agent reports state in the heartbeat and never notifies anyone itself.
- `metrics_available=False` means the agent failed to read the worker's metrics `max_metrics_failures` times in a row, so it can't judge performance. Open a `metrics_unavailable` incident in the heartbeat's transaction (idempotent, like the watcher's), and close it on the first report with `metrics_available=True`. `None` (no data yet, or the monitor is inactive) neither opens nor closes it, so an incident open when the worker stops, crashes into CRASHED_HOLD or the agent goes silent stays open until a report actually says `True`. Nobody saw the metrics recover, so nothing closes it. The sender announces opening and recovery like any incident.
- Never call an agent; there is no downward check. Silence is the signal.
- An agent is non-responding when its `last_heartbeat_at` is more than 3 ticks old (30s), by the daemon's clock. Show it as non-responding on the dashboard and otherwise ignore it: don't judge it, don't count it as healthy or unhealthy, and keep its desired state so it converges on its own when it returns. Non-responding is the cluster manager's judgement; it never overwrites the agent's `healthy`, which describes the worker.
- Transitions are rows in `incidents` (`id`, `source`, `ip`, `version`, `kind`, `opened_at`, `closed_at`, `notified_at`, `recovery_notified_at`; at most one open row per source, IP, version and kind, enforced by a partial unique index). A watcher loop runs every tick and opens a `non_responding` incident for each live agent past the threshold with none open; the agent's next heartbeat closes it in the same transaction. A sender loop posts each opening and each recovery to a Mattermost webhook after commit, outside any transaction, and marks each sent (`notified_at`, `recovery_notified_at`) only once its post succeeds. Until the webhook is configured nothing is sent. Later watchers add kinds to the same table.
- Keep operation history and its own logs. Record `last_operation` once per new `op_id`, with its `generation`. It reports only the most recent finished operation, and is `None` after an agent restart.
- An operation succeeded when `failure` is null. An `op_id` seen as `current_operation` that never shows up as `last_operation` was interrupted (the agent restarted and discarded it).
- A step with `attempts == 0` never ran. Treat `ok=true, attempts=0` explicitly as skipped by its guard: show it as skipped, not as passed, and don't count it as a run of that step (for example when asking whether `verify`, `wait_for_log` or `wait_for_version` actually ran). `ok=false, attempts=0` is a guard failure, which is a step failure like any other.
- Step names in `StepView` are the method names in the project: `start`, `stop`, `wait_for_log`, `wait_for_version`, `download`, `clear_hypertree`, `fetch_hypertree`, `reload`, `verify`, `upload_hypertree`, `reattach`.
- The report doesn't say which bundle is linked; only `running_version`, and only while RUNNING. During an UPDATE it is the old version until `reload` passes and the new one after; it can be `null` for a tick while the worker is reloading.
- Store `running_version` exactly as reported, `null` included; `process_status` says why it's `null`. The reply never depends on observed state: it is built from the desired state alone. The tagged version an agent should run is on the desired side: `desired_state.version` → `version_deployments`.
- `handled_restart_generation` and `handled_force_update_generation` in the report are always integers, never `null`.
- `process_status` is `null` while the agent hasn't attached to the worker yet.
- Report keys changed from v1: `process_status` replaces `is_running` (it was named `worker` until 2026-10-29); new `current_operation`, `last_operation`, `failed_generation` and `violations`.
