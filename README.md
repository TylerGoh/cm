# Cluster manager

Three services, each its own project folder, installed on its own machine:

| Folder | Service | Command | How many |
|---|---|---|---|
| `file-server/` | Internal file server: version artifacts and hypertrees | `file-server` | one per cluster |
| `cluster-manager/` | The daemon, and the `cm` CLI that drives it | `cm daemon`, `cm ...` | one per cluster |
| `agent/` | Supervises the worker on one box | `agent` | one per worker box |

No folder imports anything from another. Each has its own README with the steps for its box,
its own `.env.example` and `.gitignore`, and a systemd unit in `deploy/`.

## From a release to running services

Your proprietary details (the real names of the placeholder terms, the worker's commands and
metrics, what the agent sends to Elastic, and your settings) live in one `config.yaml` that you
keep yourself. It is never in this repository; `config.example.yaml` documents every key.
`prepare.py` applies it.

1. **Get the initial install**, `toolkit_cm-initial.zip` (built by `tools/make_patch.py
   --initial`; `VERSION` inside says which version it is), and unzip it.
2. **Prepare it with your config.yaml:**

   ```bash
   pip install pyyaml pydantic                # on the machine you prepare on
   cp ~/config.yaml toolkit_cm/
   python3 toolkit_cm/prepare.py --check       # validates, changes nothing
   python3 toolkit_cm/prepare.py
   ```

   It renames the placeholders (`hypertree`, `program_log`, `reload_phase` or `reload_phase1`/`reload_phase2`, `etc_dir`) to your
   names, writes the worker contract and the inventory fields into the agent, and writes each
   service's `.env` (and `cluster-manager/deploy/postgres.env`) from `config.yaml`'s `env:`
   values. A section given must have every key: a missing key, or one with no value, stops it
   before anything changes; `""` writes the key empty (per-box keys like `IPV4`).
3. **Check the wire.** `./check_wire.sh` confirms the agent's and the daemon's copies of
   `wire.py` are identical. A difference means heartbeats would fail.
4. **Fill in the per-box values** you wrote as `""`: each worker box needs its own agent `.env`
   (`IPV4`, `HOSTNAME`); where Foreman writes it, leave `env.agent` out of `config.yaml`.
5. **Copy each folder to its machine**, for example `scp -r file-server user@files-host:/opt/`.
6. **On each machine, follow that folder's README.** Each service runs in the foreground with its
   command first, then as a systemd service.

Install order the first time:
1. the file server
2. the cluster manager, whose first start fetches `BOOTSTRAP_VERSION` through the file server and fails without it
3. the agents

## Versions and patches

Releases are numbered v0, v1, v2, ..., one git tag each. The initial install is the full zip of
a version; every later version is a patch from the one before it:

```bash
python3 tools/make_patch.py        # commit first; tags HEAD v<N+1>, writes dist/patch_cm-v<N+1>.zip
git push origin v<N+1>
```

The patch zip holds `prepare.py`, an `apply.sh`, a `README.txt` with the steps, and one folder
per service that changed (`root/` for the top level), each with `old/` and `new/` copies and its
own `apply.sh`.

**Apply it to your prepared install folder** (kept in git, with `config.yaml` in it):

```bash
bash patch_cm-v<N>/apply.sh ~/toolkit_cm        # --force replaces locally changed files
cd ~/toolkit_cm && git status && git commit -am v<N>          # then push and deploy each service
```

It prepares the patch with that folder's `config.yaml`, the only one a patch ever uses (a
`config.yaml` inside the unzipped patch is refused), checks every service folder against the
previous version (outside the generated blocks), and applies them all, or changes nothing if any
check fails. `config.yaml`, `PREPARED`, the `.env` files and the `.patches/` backups are
git-ignored, so `git status` shows only the patch.

Replaced files are backed up to `<service>/.patches/vN/`; a `.env` is never touched, and keys
added or no longer used are listed. The per-service `apply.sh` files run only from the top-level
one. Then deploy each patched service from the install folder and restart it.

The agent's generated files travel with every agent patch, so a changed `config.yaml` reaches
the boxes with the next patch.

## Every machine needs

Python 3.12 with `venv`, systemd to run the service, and your internal pip index
(`PIP_INDEX_URL` or `pip.conf`): every dependency is installed by name from it, never from a URL. The units expect each folder at `/opt/<folder>` and run as root.

## How the settings connect

- The daemon's `FILE_SERVER_URL` is `http://<file server host>:<FILE_SERVER_PORT>`.
- The daemon's `FILE_SERVER_FETCH_TIMEOUT` is longer than the file server's `FILE_SERVER_UPSTREAM_TIMEOUT`.
- Each agent's `CLUSTER_MANAGER_URL` is `http://<cluster manager host>:<daemon PORT>`.
- The daemon's `NON_RESPONDING_SECONDS` is several of the agent's `HEARTBEAT_INTERVAL`.
- `CLUSTER_NAME` (sent in every Elastic document) and `INTF` (a JSON list of interfaces,
  e.g. `INTF='["eth0"]'`; one inventory record each) are required agent keys since v4: set
  them in every agent `.env`, or under `env: agent:` in your `config.yaml`.
- `HEARTBEAT_INTERVAL` is a required agent key since the release after v1: add
  `HEARTBEAT_INTERVAL: 10` under `env: agent:` in your `config.yaml` before applying that patch,
  or `prepare.py` stops.
- The daemon's `CANARY_DURATION_SECONDS` comfortably exceeds an update plus the agent's
  `PERFORMANCE_WINDOW_SECONDS`.
- Agents never need the file server's address: the daemon gives them complete URLs.

## The worker's metrics

The agent's worker contract (the worker's commands and its metrics) is generated from
`config.yaml`'s `worker:` section by `prepare.py`, not written by hand.
`docs/worker-contract.md` says how the performance judgement reads the metrics, with a sample.

## Docs

`docs/architecture.md` is the current design, `docs/decisions.md` says why, and
`docs/open-items.md` lists what is unresolved.
