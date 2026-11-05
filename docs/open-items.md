# Open items

Unresolved questions and undesigned parts. When one is settled, move the outcome and its reason into `decisions.md` and update `architecture.md`.

## Open questions

- If a worker payload is nested rather than flat, `WorkerClient._get_json` needs a flatten step.
- Whether START counts as updating (`UPDATING_OPS` is UPDATE and FORCE_UPDATE today).
- With no routes, FastAPI and uvicorn only host the lifespan. Decide whether to keep them (signal handling, graceful shutdown, room for routes later) or run the lifespan under plain `asyncio.run`. Related: uvicorn runs the lifespan, and so the first tick, before it binds the port, so a second agent started by hand acts once (a heartbeat, possibly a REATTACH) before it fails to bind. systemd prevents that in practice; a lock file in the lifespan would prevent it everywhere. Running `agent` in a terminal while the service runs is now an easy way to hit this.
- What the `agent_started` document contains beyond `event`, `at`, `agent_version` and the host fields: deployment settings, whether it was a first boot.
- The synchronous file reads and fsyncs in `JsonStore` and `ProcessManager.derive_status` block the event loop and so delay HTTP requests. Move them to a thread if that ever matters.
- If the worker ever runs as a systemd unit, consider a systemd watchdog (`WatchdogSec=`, `Restart=no`) for hang detection, with `ProcessManager` mapping the unit's `failed` state to CRASHED and the health monitor going back to reporting only.

## Packaging

The three projects are split, installed by hand from their READMEs, and kept in `TylerGoh/cm` (2026-11-04 entries); the rename checklist, the wire check and the metrics generator are written (2026-11-05 entries). Still open:

- `check_wire.sh` is run by hand; nothing runs it automatically (no CI yet).
- There are no tests for the cluster manager's CLI. Its renderers return Rich renderables, which a test captures with `Console(record=True)` and compares with `export_text()`.

## Process and update managers

Both are written out (2026-11-04 entries). Still open:

- The worker's real paths and metrics aren't in the code yet; the contract block is still the mock one, to be regenerated with `tools/metrics_generator` (the reload phases are confirmed as blocking GETs with no body).
- `download` swaps the `etc_dir` links one by one, not as a set: an agent that dies part-way leaves some links on the new bundle and some on the old until the next `download`.
- Unpacking drops Unix permissions. Nothing in a bundle can rely on an executable bit.
- Old console logs are never deleted by the agent; `/tmp` cleaning bounds them.

## Cluster manager alignment

Identity, the reply and the `DesiredState` shape are settled (2026-11-01 entries). Still to work through:

- Cluster-manager-only behaviour: watchers (CRASHED_HOLD, `failed_generation == generation`, `acknowledged_generation > generation`), the per-agent success checklist, the force-bump and crashed-rollback rules, the `max()` on handled counters versus re-provisioning, and the watcher queries including decommissioned agents.

## Canary

The canary is designed and written (2026-11-02 and 2026-11-04 entries). Still open:

- NOT_CONVERGED probably needs its own handler, and maybe a warning, rather than only counting toward a rejection. For now it rejects the canary past `canary_max_not_converged`.
- The limits are fixed counts, but the sample can shrink. With a sample of one and `canary_max_crashes` at one, a canary can't fail on crashes.

## Cluster manager daemon and client

The daemon/client split, the dispatch flow and the daemon's wiring are written (2026-11-02 and 2026-11-04 entries). Still open:

- The name and placement rule of the shared schemas module, the counterpart of `wire.py`. It is `cluster_manager/schemas.py` today.
- The CLI (`schemas.py`, `client/`, `api/fleet.py`, `api/agents.py`, `services/views.py`, `services/conditions.py`, `services/desired_state.py`) works against the daemon but is still waiting for the owner's review. It now runs on Typer and Rich.
- Whether the daemon records a failed dispatch. A failed fetch leaves nothing on the file server; a fetched job whose dispatch then finds no eligible agent stays on the file server, and a later dispatch of it reuses it, since the fetch is idempotent.
- Whether the upstream artifact's integrity is checked against something upstream publishes. The file server checks that it is a valid zip and its sha256 covers the file server → agent leg, but nothing checks that it is the artifact upstream meant.

## File server

The file server is designed and written (2026-11-04 entry), and keeps every job (2026-11-05 entry). Still open:

- `hypertree_get_url` and `hypertree_upload_url` always carry the same URL. Collapsing them into one `hypertree_url` field is a wire change, deployed to both sides together.

## Hypertree

Clearing, fetching and uploading in UPDATE (2026-11-04 entries) and clearing and fetching in FORCE_UPDATE (2026-11-05 entry) are settled. Still open:

- The retries for `clear_hypertree`, `fetch_hypertree` and `upload_hypertree`. They keep the default (3) for now, and every retry of a step that times out adds its full timeout to the update.

## Cluster manager database

Every model and repository is written, and runs on PostgreSQL (2026-11-02, 2026-11-04 and 2026-11-05 entries). Still open:

- Whether `DesiredStateRepo.apply` should write the history row itself, so a service can't forget it. Today the service writes it from the returned row.
- Whether interrupted operations are recorded. `operations` only gets rows from `last_operation`, so an `op_id` seen in `current_operation` that never finishes leaves no row.
- The incident repository is taken as a first draft; the owner will revisit it now that the watcher and sender exist.
- When to start using migrations. The schema is created with `create_all` at startup while the data can be wiped.

## Not designed yet

- `docs/metrics-draft.md` is fully superseded (by `performance.py` and the metrics exporter) and can be deleted.
- Alerting, to be added later. Candidates so far: an automatic rollback (today it is only logged and recorded in `unstable_reason`), an ACTIVE that came from a rollback breaching the fleet limits (it isn't rolled back, and nothing says so), and canary agents that didn't converge.
- `cm status` doesn't show the fleet watch's verdict or a breach in progress; only the daemon's log does.
- The worker's real command names and `WorkerMetrics` fields, and the performance judgement over them (the owner codes it). `agent/worker_client.py` uses mock ones (`show_version`, `counters`, `reload_phase1`, `reload_phase2`), each appended as-is to `WORKER_URL`.
- `tests/test_decide.py` beyond the force bump, `tests/test_pipeline.py`, tests for the daemon beyond the canary judgement, and `tests/integration/`.
