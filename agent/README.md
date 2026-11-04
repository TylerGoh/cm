# agent

Supervises the worker on one box. Every 10 seconds it reports to the cluster manager and gets
back the desired state, then starts, stops, updates or restarts the worker to match. It serves
no routes. One per worker box.

## What the box needs

- Python 3.12 with `venv`, and a pip index (PyPI, or `PIP_INDEX_URL`).
- The worker's default install: the binary in `WORKER_COMMAND`, `ETC_DIR` and `HYPERTREE_DIR`.
  The agent never installs the worker.
- The Elastic CA certificate at `ELASTIC_CERT_PATH`. The agent won't start without it.
- Network access to the cluster manager, the file server, Elastic and the worker's HTTP port.

## Install

```bash
cd /opt/agent                       # this folder, copied to the box
python3.12 -m venv .venv
.venv/bin/pip install -e .          # editable: config.py reads the .env next to it
cp .env.example .env                # fill it in (Foreman writes it on provisioned boxes)
mkdir -p /var/lib/agent             # the folder of MEMORY_STORE_PATH and PROCESS_STORE_PATH
.venv/bin/agent                     # runs in the foreground; Ctrl-C stops it
```

A missing, misspelled or invalid setting fails at startup with the key's name. The agent
creates `DOWNLOAD_DIR`, `BUNDLE_DIR` and `WORKER_CONSOLE_LOG_DIR` itself.

## Run it as a service

```bash
cp deploy/agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agent
journalctl -u agent -f
```

The unit expects this folder at `/opt/agent`.

## Good to know

- **The worker outlives the agent.** Stopping or restarting the agent leaves the worker
  running (`KillMode=process`), and the next agent reattaches to it. To keep the worker down,
  use `cm stop <ip>` on the cluster manager.
- **One agent per box.** Don't run `agent` in the foreground while the service runs: both
  would drive the same worker.
- **State** is two small files, `MEMORY_STORE_PATH` and `PROCESS_STORE_PATH`. A missing file
  means first boot. A corrupt one fails startup on purpose.
- **Worker console logs**: one file per spawn in `WORKER_CONSOLE_LOG_DIR`, named
  `worker-console-<UTC time>.log`. The path is also in the journal.
- **Settings changes** need a restart.
- **Upgrading**: replace everything in this folder except `.venv` and `.env`, run
  `.venv/bin/pip install -e .` again, then restart.
- **Tests**: `.venv/bin/pip install -e '.[dev]'`, then `.venv/bin/pytest`.

## Layout

```
config.py          settings, read only from .env next to it
agent/             the agent (the `agent` command runs agent/__main__.py)
tests/             pytest
deploy/agent.service
.env.example       every setting, with example values
```
