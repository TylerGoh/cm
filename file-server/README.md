# file-server

The internal file server. It stores each version's artifact (upstream calls a version a job),
which it fetches from upstream when the daemon dispatches the version, and each job's hypertree,
which the first agent to load the job uploads. Agents download artifacts only from here. Internal
network only, no authentication. One per cluster.

## What the box needs

- Python 3.12 with `venv`, and your internal pip index (`PIP_INDEX_URL` or `pip.conf`); every dependency is installed by name from it, never from a URL.
- Network access to upstream, where artifacts are fetched from.
- Disk space at `FILE_SERVER_ROOT`: nothing is ever deleted, so it grows with every job.

## Install

```bash
cd /opt/file-server                 # this folder, copied to the box
python3.12 -m venv .venv
.venv/bin/pip install -e .          # editable: the settings are read from the .env in this folder
cp .env.example .env                # fill it in
mkdir -p /srv/files                 # FILE_SERVER_ROOT
.venv/bin/file-server               # runs in the foreground; Ctrl-C stops it
```

In another shell, `curl http://localhost:8080/jobs/none` should answer `404 {"detail":"no job none"}`.

A missing, misspelled or invalid setting fails at startup with the key's name.

## Run it as a service

```bash
cp deploy/file-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now file-server
journalctl -u file-server -f
```

The unit expects this folder at `/opt/file-server`.

## Good to know

- **Install it first.** The cluster manager's first start fetches its bootstrap version through
  the file server.
- **Point the daemon at it**: the daemon's `FILE_SERVER_URL` is `http://<this host>:<FILE_SERVER_PORT>`,
  and its `FILE_SERVER_FETCH_TIMEOUT` must be longer than `FILE_SERVER_UPSTREAM_TIMEOUT` here.
- **Storage**: `<FILE_SERVER_ROOT>/<job_id>/artifact`, `job.json` (written last, so a job only
  exists once complete) and `hypertree`.
- **One process on purpose**: the per-job locks live in memory.
- **Upgrading**: replace everything in this folder except `.venv` and `.env`, run
  `.venv/bin/pip install -e .` again, then restart.

## Layout

```
file_server/file_server.py   the whole server (the `file-server` command)
deploy/file-server.service
.env.example                 every setting, with example values
```
