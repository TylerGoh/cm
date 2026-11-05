# cluster-manager

The cluster manager daemon and its CLI, `cm`. The daemon answers every agent's heartbeat with
that agent's desired state, dispatches and canaries new versions, and posts incidents to
Mattermost. One per cluster.

## What the box needs

- Python 3.12 with `venv`, and a pip index (PyPI, or `PIP_INDEX_URL`).
- Podman with Quadlet (podman 4.4 or later) and the Bitnami PostgreSQL image, for the database.
- The file server running and reachable at `FILE_SERVER_URL`. The daemon's first start fetches
  `BOOTSTRAP_VERSION` through it, and fails without it.

## Install

First the database: PostgreSQL in a podman container, run by systemd. Set `Image=` in
`deploy/cluster-manager-postgres.container` to the image as `podman images` lists it.

```bash
cd /opt/cluster-manager             # this folder, copied to the box
mkdir -p /etc/cluster-manager
cp deploy/postgres.env.example /etc/cluster-manager/postgres.env    # fill in the passwords
chmod 600 /etc/cluster-manager/postgres.env
cp deploy/cluster-manager-postgres.container /etc/containers/systemd/
systemctl daemon-reload
systemctl start cluster-manager-postgres    # enabled at boot by its [Install] section
podman healthcheck run cluster-manager-postgres && echo ready      # once it's initialised
```

Then the daemon:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .          # editable: the daemon's config reads the .env in this folder
cp .env.example .env                # fill it in; DATABASE_URL carries the password from postgres.env
.venv/bin/cm daemon                 # runs the daemon in the foreground; Ctrl-C stops it
```

In another shell, `.venv/bin/cm status` should show the bootstrap version as ACTIVE.

A missing, misspelled or invalid setting fails at startup with the key's name.

## Run it as a service

```bash
cp deploy/cluster-manager.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cluster-manager
journalctl -u cluster-manager -f
ln -s /opt/cluster-manager/.venv/bin/cm /usr/local/bin/cm     # cm on the PATH
```

The unit runs `cm daemon`, expects this folder at `/opt/cluster-manager`, and starts after the
database. If the database isn't accepting connections yet, the daemon fails and systemd restarts it
5 seconds later.

## cm

`cm` talks to the daemon at `http://127.0.0.1:8000` by default. Set `CM_DAEMON_URL` or pass
`--daemon-url` for another host or port. Only `cm daemon` needs `.env`, so `cm` can run from any
machine that reaches the daemon, after the same `pip install -e .`.

```bash
cm --help                       # every command; cm <command> --help for its options
cm status                       # is the fleet OK?
cm agents --problems            # agents that aren't converged or stopped
cm agent 10.0.12.34             # everything about one agent
cm dispatch job_42 https://upstream.internal/jobs/job_42/optimized
cm canary                       # the running canary, as of now
cm rollback
cm restart | force-update | stop | start | decommission <ip>
cm --json agents                # global options go before the command
```

## Good to know

- **The database** is PostgreSQL in the `cluster-manager-postgres` container, its data in the
  podman volume `cluster-manager-pgdata`. There are no migrations yet: if an upgrade changes a
  model, wipe it and start again:

  ```bash
  systemctl stop cluster-manager cluster-manager-postgres
  podman volume rm cluster-manager-pgdata
  systemctl start cluster-manager-postgres cluster-manager
  ```

  The first boot seeds `BOOTSTRAP_VERSION` again, and agents re-register on their next heartbeat.
  Set `BOOTSTRAP_VERSION` and `BOOTSTRAP_UPSTREAM_URL` to the version that should be ACTIVE
  first, or every agent is pointed back at the old bootstrap version.
- **Moving from SQLite**: nothing is copied over. Treat it as a wipe (above): set the bootstrap
  keys to the version that's ACTIVE today, then start on PostgreSQL.
- **A psql shell**: `podman exec -it cluster-manager-postgres psql -U cluster_manager -d cluster_manager`.
- **`BOOTSTRAP_VERSION` and `BOOTSTRAP_UPSTREAM_URL`** are read on the first boot only.
- **Mattermost**: with an empty `MATTERMOST_WEBHOOK_URL` nothing is sent. Incidents wait, and go
  out once it is set and the daemon restarted.
- **Automatic rollback**: a promoted version is judged every 10 seconds for as long as it is
  ACTIVE, by the canary's rules with the `FLEET_MAX_*` limits. If it stays over a limit for
  `FLEET_BREACH_SECONDS`, the daemon rolls back to the latest STABLE version on its own;
  `cm versions` shows why. An existing `.env` needs the four `FLEET_*` keys added, or the daemon
  won't start.
- **One process only**: the canary scheduler and the loops live in it.
- **Upgrading**: replace everything in this folder except `.venv` and `.env`, run
  `.venv/bin/pip install -e .` again, then restart.

## Layout

```
cluster_manager/schemas.py   the cm <-> daemon contract
cluster_manager/daemon/      the daemon (cm daemon)
cluster_manager/client/      the cm CLI
deploy/cluster-manager.service
deploy/cluster-manager-postgres.container   the database (Quadlet)
deploy/postgres.env.example                 its settings
.env.example                 every setting, with example values
```
