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
2. **Rename the placeholders.** `hypertree`, `etc_dir` and `program_log` stand in for the real
   names. Fill in the real ones in the checklist at the top of `rename_term.sh`, then run it,
   before filling in any `.env`, so the `.env.example` keys already carry the real names:

   ```bash
   ./rename_term.sh --dry-run     # shows what would change
   ./rename_term.sh
   ```

   Renaming `hypertree` changes the agent ↔ daemon wire and the file server's paths, so all three
   must be reinstalled together.
3. **Check the wire.** `./check_wire.sh` confirms the agent's and the daemon's copies of
   `wire.py` are identical. A difference means heartbeats would fail.
4. **Fill in the settings.** In each folder, `cp .env.example .env` and fill it in. Every key is
   required and no others are allowed. Each worker box needs its own agent `.env` (`IPV4`,
   `HOSTNAME`, `LOCATION`); where Foreman writes it, leave it out here.
5. **Copy each folder to its machine**, for example `scp -r file-server user@files-host:/opt/`.
6. **On each machine, follow that folder's README.** Each service runs in the foreground with its
   command first, then as a systemd service.

Install order the first time:
1. the file server
2. the cluster manager, whose first start fetches `BOOTSTRAP_VERSION` through the file server and fails without it
3. the agents

## Versions and patches

Releases are numbered v0, v1, v2, ..., one git tag each. v0 is the fresh install: the full zip
of the `v0` tag. Each later version is a patch from the version before it:

```bash
python3 tools/make_patch.py        # commit first; tags HEAD v<N+1>, writes dist/cluster_manager-v<N+1>-patch.zip
git push origin v<N+1>
```

The zip holds `patch-v<N+1>.sh` and `rename_term.sh`. On each box, unzip it in the home folder and
run `bash patch-v<N+1>/patch-v<N+1>.sh`, then restart the services. The patch applies the box's
renamed names (from its `rename_term.sh` checklist) and changes nothing unless every file is
still at the previous version. It backs up the old files under `<project>/.patches/` and never
touches `.env` files. `dist/` is git-ignored.

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

## The worker's metrics

The agent's worker contract (the worker's commands and its metrics) is generated, not written by
hand: describe the worker in YAML and run `tools/metrics_generator/generate.py`. Its README says
how to paste the result into the agent, and has a sample performance judgement.

## Docs

`docs/architecture.md` is the current design, `docs/decisions.md` says why, and
`docs/open-items.md` lists what is unresolved.
