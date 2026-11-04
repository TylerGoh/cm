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

1. **Get a release.** `git archive -o release.zip HEAD` from a clone, then unzip it. Work in the
   top folder, the one holding this README.
2. **Rename aliases, if any.** Some names in the code stand in for the real ones. Rename them
   before filling in any `.env`, so the `.env.example` keys already carry the real names:

   ```bash
   ./rename_term.sh --dry-run hypertree speedtree     # shows what would change
   ./rename_term.sh hypertree speedtree
   ```

   Renaming `hypertree` changes the agent ↔ daemon wire and the file server's paths, so all three
   must be reinstalled together.
3. **Fill in the settings.** In each folder, `cp .env.example .env` and fill it in. Every key is
   required and no others are allowed. Each worker box needs its own agent `.env` (`IPV4`,
   `HOSTNAME`, `LOCATION`); where Foreman writes it, leave it out here.
4. **Copy each folder to its machine**, for example `scp -r file-server user@files-host:/opt/`.
5. **On each machine, follow that folder's README.** Each service runs in the foreground with its
   command first, then as a systemd service.

Install order the first time:
1. the file server
2. the cluster manager, whose first start fetches `BOOTSTRAP_VERSION` through the file server and fails without it
3. the agents

## Every machine needs

Python 3.12 with `venv`, a pip index (PyPI, or `PIP_INDEX_URL`), and systemd to run the
service. The units expect each folder at `/opt/<folder>` and run as root.

## How the settings connect

- The daemon's `FILE_SERVER_URL` is `http://<file server host>:<FILE_SERVER_PORT>`.
- The daemon's `FILE_SERVER_FETCH_TIMEOUT` is longer than the file server's `FILE_SERVER_UPSTREAM_TIMEOUT`.
- Each agent's `CLUSTER_MANAGER_URL` is `http://<cluster manager host>:<daemon PORT>`.
- The daemon's `CANARY_DURATION_SECONDS` comfortably exceeds an update plus the agent's
  `PERFORMANCE_WINDOW_SECONDS`.
- Agents never need the file server's address: the daemon gives them complete URLs.

## Docs

`docs/architecture.md` is the current design, `docs/decisions.md` says why, and
`docs/open-items.md` lists what is unresolved.
