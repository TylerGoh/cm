# Open items

Unresolved questions and undesigned parts. When one is settled, move the outcome and its reason into `decisions.md` and update `architecture.md`.

## Open questions

- The `metrics()` missing-name warning logs on every reading while an endpoint is broken. Log only the first occurrence and the recovery if that's too noisy.
- If a worker payload is nested rather than flat, `WorkerClient._get_json` needs a flatten step.
- Whether START counts as updating (`UPDATING_OPS` is UPDATE and FORCE_UPDATE today).
- With no routes, FastAPI and uvicorn only host the lifespan. Decide whether to keep them (signal handling, graceful shutdown, room for routes later) or run the lifespan under plain `asyncio.run`. Related: uvicorn runs the lifespan, and so the first tick, before it binds the port, so a second agent started by hand acts once (a heartbeat, possibly a REATTACH) before it fails to bind. systemd prevents that in practice; a lock file in the lifespan would prevent it everywhere. Running `agent` in a terminal while the service runs is now an easy way to hit this.
- What the `agent_started` document contains beyond `event`, `at`, `agent_version` and the host fields: deployment settings, whether it was a first boot.
- `JsonStore.save` doesn't fsync the directory after `os.replace`, so the rename isn't guaranteed to survive a power loss.
- The synchronous file reads and fsyncs in `JsonStore` and `ProcessManager.derive_status` block the event loop and so delay HTTP requests. Move them to a thread if that ever matters.
- Rename `metrics.py` to `performance.py`, since it holds `PerformanceMonitor` and the raw metrics live in `worker_client.py`.
- If the worker ever runs as a systemd unit, consider a systemd watchdog (`WatchdogSec=`, `Restart=no`) for hang detection, with `ProcessManager` mapping the unit's `failed` state to CRASHED and the health monitor going back to reporting only.

## Packaging

The three projects are split, installed by hand from their READMEs, and kept in `TylerGoh/cm` (2026-11-04 entries). Still open:

- Nothing checks that the two `wire.py` copies (`agent/agent/wire.py` and `cluster-manager/cluster_manager/daemon/wire.py`) stay identical, now that they live in separate projects and no single install or test sees both. A check at the repository top (a script or CI step comparing the two files) would catch drift before a deploy.
- **The rename script becomes a checklist** (to discuss). Instead of renaming any term given on the command line, `rename_term.sh` should carry a fixed list of the placeholder names the owner has said aren't the real ones (`hypertree`, `etc_dir`, and a third still to be named) and rename only those.
- **A generator for the worker's metrics contract** (to discuss). A script that reads a YAML file describing the worker's endpoints and metrics (for each metric: its name at the endpoint, its field name, counter or gauge) and writes paste-ready Python into temporary files in its own folder: the contract block for `worker_client.py` and the `WorkerMetrics` fields. The performance judgement is not generated; the owner codes it, and the generator's README carries a sample judgement (today's failed/intake ratio) to follow.
- Every machine needs a pip index (PyPI or `PIP_INDEX_URL`). A box with no index would need wheels shipped in the release (`pip download`, then `pip install --no-index --find-links`).
- Every unit runs as root (no `User=`). Whether the cluster manager and file server should run as a service user.
- There are no tests for the cluster manager's CLI. Its renderers return Rich renderables, which a test captures with `Console(record=True)` and compares with `export_text()`.

## Process and update managers

Both are written out (2026-11-04 entries). Still open:

- Step names: the cluster manager now sees `start` and `stop` in `StepView`, where the earlier private implementation reported `spawn_process` and `kill_instance`. Rename the methods if the old names must stay visible.
- The worker's reload phases are assumed to be GETs with no body, with phase 1 returning only once phase 2 can be called. Confirm against the real worker.
- `download` swaps the `etc_dir` links one by one, not as a set: an agent that dies part-way leaves some links on the new bundle and some on the old until the next `download`.
- A link whose entry is later removed from `bundle_links` is left behind, pointing into a bundle that the next `download` deletes. Nothing removes stale links today.
- Unpacking drops Unix permissions. Nothing in a bundle can rely on an executable bit.
- Old console logs are never deleted by the agent; `/tmp` cleaning bounds them.

## Cluster manager alignment

Identity, the reply and the `DesiredState` shape are settled (2026-11-01 entries). Still to work through:

- Cluster-manager-only behaviour: watchers (CRASHED_HOLD, `failed_generation == generation`, `acknowledged_generation > generation`), the per-agent success checklist, the force-bump and crashed-rollback rules, the `max()` on handled counters versus re-provisioning, and the watcher queries including decommissioned agents.
- The higher post-promotion threshold: what it measures (probably the same failed ratio with a higher limit), its value, and whether it triggers the rollback automatically or through the client's roll-back request. A rollback can only be requested by a human today.

## Canary

The canary is designed and written (2026-11-02 and 2026-11-04 entries). Still open:

- NOT_CONVERGED probably needs its own handler, and maybe a warning, rather than only counting toward a rejection. For now it rejects the canary past `canary_max_not_converged`.
- Whether a canary with no converged agent can pass. With `canary_max_not_converged` at or above the sample size, a canary where nothing converged would be promoted on no evidence. The same holds when every canary agent was decommissioned before the deadline: no outcomes, nothing over a limit, promoted.
- The limits are fixed counts, but the sample can shrink. With a sample of one and `canary_max_crashes` at one, a canary can't fail on crashes.
- Whether `metrics_available=False` at the canary deadline should be its own outcome rather than NO_VERDICT. Such an agent can't vouch for the version's performance.

## Cluster manager daemon and client

The daemon/client split, the dispatch flow and the daemon's wiring are written (2026-11-02 and 2026-11-04 entries). Still open:

- The name and placement rule of the shared schemas module, the counterpart of `wire.py`. It is `cluster_manager/schemas.py` today.
- The CLI (`schemas.py`, `client/`, `api/fleet.py`, `api/agents.py`, `services/views.py`, `services/conditions.py`, `services/desired_state.py`) works against the daemon but is still waiting for the owner's review. It now runs on Typer and Rich.
- Whether the daemon records a failed dispatch. A failed fetch leaves nothing on the file server; a fetched job whose dispatch then finds no eligible agent stays on the file server, and a later dispatch of it reuses it, since the fetch is idempotent.
- Whether the upstream artifact's integrity is checked against something upstream publishes. The file server checks that it is a valid zip and its sha256 covers the file server → agent leg, but nothing checks that it is the artifact upstream meant.

## File server

The file server is designed and written (2026-11-04 entry). Still open:

- Nothing is ever deleted, so the file server's disk grows with every job. Removing UNSTABLE versions, or all but the newest few, would need a delete endpoint the daemon calls.
- `hypertree_get_url` and `hypertree_upload_url` always carry the same URL. Collapsing them into one `hypertree_url` field is a wire change, deployed to both sides together.

## Hypertree

Clearing, fetching and uploading in UPDATE is settled (2026-11-04 entries). Still open:

- Whether FORCE_UPDATE fetches the hypertree after `download`, and whether CRASH_RESTART uploads one. A force-updated bundle is first loaded by the CRASH_RESTART that follows, so today its hypertree is neither fetched before that load nor uploaded after it.
- The retries for `clear_hypertree`, `fetch_hypertree` and `upload_hypertree`. They keep the default (3) for now, and every retry of a step that times out adds its full timeout to the update.

## Cluster manager database

Every model and repository is written and ported to SQLite (2026-11-02 and 2026-11-04 entries). Still open:

- Whether `DesiredStateRepo.apply` should write the history row itself, so a service can't forget it. Today the service writes it from the returned row.
- Whether interrupted operations are recorded. `operations` only gets rows from `last_operation`, so an `op_id` seen in `current_operation` that never finishes leaves no row.
- The incident repository is taken as a first draft; the owner will revisit it now that the watcher and sender exist.
- When to start using migrations. The schema is created with `create_all` at startup while the data can be wiped.

## Not designed yet

- What goes into a metrics document for Elastic. `ElasticClient.index_metrics` and the metrics index exist; nothing calls them. `docs/metrics-draft.md` is superseded by `metrics.py` and can be deleted once nothing in it is wanted (the Elastic document idea is the only piece not carried over).
- Alerting, to be added later. Candidates so far: an ACTIVE that came from a rollback breaching the higher threshold (it isn't rolled back), and canary agents that didn't converge.
- The worker's real endpoint paths and `WorkerMetrics` fields. `agent/worker_client.py` uses mock ones (`/show_version`, `/counters`, `/reload_phase1`, `/reload_phase2`).
- `tests/test_decide.py`, `tests/test_pipeline.py`, tests for the daemon, and `tests/integration/`.
