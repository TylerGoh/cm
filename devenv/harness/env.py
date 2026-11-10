"""The mock cluster: starts, resets and tears down every process, all under devenv/.run/.

Base state: PostgreSQL (empty), the fake upstream, the file server and the daemon, which boots
with job_1 ACTIVE and no agents. A scenario then starts two agents; each spawns a fake worker.

Every service runs from a fresh copy of its project folder (.run/<name>/src) with its own .env,
because each service reads .env from next to its code. Nothing in the real project folders is
written. Every process we start is its own process group, recorded in .run/pids/<name>.pid; the
fake workers (spawned by the agents) write .run/pids/worker-<port>.pid themselves. Every process
also carries DEVENV_RUN=<.run path> in its environment, so a sweep finds strays from a run that
was killed with kill -9.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
from dotenv import dotenv_values

HARNESS = Path(__file__).resolve().parent
DEVENV = HARNESS.parent
REPO = DEVENV.parent
RUN = DEVENV / ".run"
PIDS = RUN / "pids"
LOGS = RUN / "logs"
MARKER = "DEVENV_RUN"
PYTHON = sys.executable

# Every port in 54xxx, so anything there belongs to the dev environment.
DAEMON_PORT = 54000
FILE_SERVER_PORT = 54080
UPSTREAM_PORT = 54090
POSTGRES_PORT = 54432
ELASTIC_PORT = 54999            # nothing listens: Elastic writes fail quietly, as the agent allows
AGENT_PORTS = (54101, 54102)
WORKER_PORTS = (54201, 54202)
ALL_PORTS = (DAEMON_PORT, FILE_SERVER_PORT, UPSTREAM_PORT, POSTGRES_PORT, ELASTIC_PORT,
             *AGENT_PORTS, *WORKER_PORTS)

DAEMON_URL = f"http://127.0.0.1:{DAEMON_PORT}"
UPSTREAM_URL = f"http://127.0.0.1:{UPSTREAM_PORT}"
BOOTSTRAP_VERSION = "job_1"
AGENT_IPS = ("10.99.0.1", "10.99.0.2")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The daemon's dev settings: two agents, so canary size 1 and every limit 0, short waits.
DAEMON_ENV = {
    "PORT": DAEMON_PORT, "LOG_LEVEL": "INFO", "LOG_FORMAT": LOG_FORMAT,
    "DATABASE_URL": f"postgresql+asyncpg://cm@127.0.0.1:{POSTGRES_PORT}/cluster_manager",
    "FILE_SERVER_URL": f"http://127.0.0.1:{FILE_SERVER_PORT}",
    "FILE_SERVER_TIMEOUT": 10, "FILE_SERVER_FETCH_TIMEOUT": 60,
    "BOOTSTRAP_VERSION": BOOTSTRAP_VERSION,
    "BOOTSTRAP_UPSTREAM_URL": f"{UPSTREAM_URL}/jobs/{BOOTSTRAP_VERSION}",
    "NON_RESPONDING_SECONDS": 30,
    "CANARY_SIZE": 1, "CANARY_DURATION_SECONDS": 120,
    "CANARY_MAX_CRASHES": 0, "CANARY_MAX_VIOLATIONS": 0, "CANARY_MAX_NOT_CONVERGED": 0,
    "FLEET_MAX_CRASHES": 0, "FLEET_MAX_VIOLATIONS": 0, "FLEET_MAX_NOT_CONVERGED": 0,
    "FLEET_BREACH_SECONDS": 15,
    "MATTERMOST_WEBHOOK_URL": "", "MATTERMOST_TIMEOUT": 5,
}

FILE_SERVER_ENV = {
    "FILE_SERVER_ROOT": str(RUN / "file-server" / "files"),
    "FILE_SERVER_UPSTREAM_TIMEOUT": 30,
    "FILE_SERVER_PORT": FILE_SERVER_PORT,
}


def agent_env(n: int) -> dict[str, object]:
    """Agent n's settings (n = 1 or 2): fast monitors, a 30s performance window."""
    home = RUN / f"agent{n}"
    worker_port = WORKER_PORTS[n - 1]
    return {
        "IPV4": AGENT_IPS[n - 1], "HOSTNAME": f"dev-agent-{n}",
        "PORT": AGENT_PORTS[n - 1], "LOG_LEVEL": "INFO", "LOG_FORMAT": LOG_FORMAT,
        "MEMORY_STORE_PATH": str(home / "state" / "memory.json"),
        "PROCESS_STORE_PATH": str(home / "state" / "process.json"),
        "WORKER_COMMAND": [PYTHON, str(HARNESS / "fake_worker.py"), "--port", str(worker_port),
                           "--log", str(home / "worker" / "worker.log"),
                           "--etc", str(home / "worker" / "etc"),
                           "--hypertree", str(home / "worker" / "hypertree"),
                           "--pids", str(PIDS)],
        "WORKER_CONSOLE_LOG_DIR": str(home / "worker" / "console"),
        "WORKER_PROGRAM_LOG_PATH": str(home / "worker" / "worker.log"),
        "WORKER_INITIALIZE_FLAG": "worker ready",
        "START_TIMEOUT": 60, "STOP_TIMEOUT": 10,
        "WORKER_URL": f"http://127.0.0.1:{worker_port}/api?command=",
        "WORKER_TIMEOUT": 2, "RELOAD_TIMEOUT": 60,
        "DOWNLOAD_DIR": str(home / "downloads"),
        "BUNDLE_DIR": str(home / "bundles"),
        "ETC_DIR": str(home / "worker" / "etc"),
        "BUNDLE_SYMLINK_MAPPING": {"app/version.txt": "app/version.txt"},
        "BUNDLE_SYMLINK_DEFAULT_DIR": "defaults",
        "DOWNLOAD_TIMEOUT": 60,
        "HYPERTREE_DIR": str(home / "worker" / "hypertree"),
        "HYPERTREE_TIMEOUT": 30, "VERIFY_TIMEOUT": 30, "VERIFY_INTERVAL": 1,
        "HEALTH_INTERVAL": 2, "MAX_FAILURES": 3,
        "PERFORMANCE_INTERVAL": 3, "PERFORMANCE_WINDOW_SECONDS": 30, "MAX_FAILED_RATIO": 0.30,
        "MAX_METRICS_FAILURES": 3, "INVENTORY_INTERVAL": 60, "REQUIRE_RUNNING": "false",
        "METRICS_EXPORT_INTERVAL": 60,
        "CLUSTER_MANAGER_URL": DAEMON_URL,
        "HEARTBEAT_INTERVAL": 10,
        "ELASTIC_URL": f"https://127.0.0.1:{ELASTIC_PORT}",
        "ELASTIC_CERT_PATH": str(RUN / "elastic" / "elastic.cert"),
        "ELASTIC_API_KEY": "ZGV2OmRldg==",
        "ELASTIC_METRICS_INDEX": "dev-metrics", "ELASTIC_HOST_INDEX": "dev-hosts",
        "ELASTIC_TIMEOUT": 1,
    }


class EnvError(RuntimeError):
    pass


Report = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


# --- small helpers ---

def write_env(project: str, dest: Path, values: dict[str, object]) -> None:
    """Writes dest/.env with exactly the keys of the project's .env.example, so a key the
    project adds or drops is caught here rather than as a startup failure."""
    example = REPO / project / ".env.example"
    keys = set(dotenv_values(example))
    missing, extra = keys - values.keys(), values.keys() - keys
    if missing or extra:
        raise EnvError(f"{example} and devenv/harness/env.py disagree: devenv is missing "
                       f"{sorted(missing)}, has extra {sorted(extra)}")
    lines = []
    for key, value in values.items():
        text = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        lines.append(f"{key}='{text}'")
    (dest / ".env").write_text("\n".join(lines) + "\n")


def copy_source(project: str, dest: Path) -> None:
    shutil.copytree(REPO / project, dest, ignore=shutil.ignore_patterns(
        ".venv", "venv", "__pycache__", ".pytest_cache", "*.egg-info", ".env", ".patches",
        "tests", "deploy", "build", "dist"))


def port_holder(port: int) -> str | None:
    """Who listens on the port, or None when it's free."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return None
        except OSError:
            pass
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                if conn.pid:
                    proc = psutil.Process(conn.pid)
                    return f"pid {conn.pid} ({' '.join(proc.cmdline())[:80]})"
    except (psutil.Error, OSError):
        pass
    return "another process (not ours to name)"


def wait_for(what: str, check: Callable[[], bool], timeout: float, proc: subprocess.Popen | None = None,
             log: Path | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            tail = log.read_text()[-1500:] if log and log.exists() else ""
            raise EnvError(f"{what} exited with {proc.returncode}\n{tail}")
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.3)
    tail = log.read_text()[-1500:] if log and log.exists() else ""
    raise EnvError(f"{what} not ready after {timeout:.0f}s\n{tail}")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pg_bin() -> Path:
    spec = importlib.util.find_spec("pgserver")
    if spec is None or not spec.submodule_search_locations:
        raise EnvError("pgserver isn't installed in devenv/.venv (delete it and run ./devenv/dev)")
    return Path(next(iter(spec.submodule_search_locations))) / "pginstall" / "bin"


def is_ours(proc: psutil.Process) -> bool:
    try:
        return proc.environ().get(MARKER) == str(RUN)
    except (psutil.Error, OSError):
        return False


def kill_group(pid: int, sig: signal.Signals) -> bool:
    """Signals pid's process group (pid leads it). False when it's already gone."""
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def wait_gone(pids: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE]
        if alive:
            time.sleep(0.1)
    return alive


# --- the environment ---

@dataclass
class Service:
    name: str
    proc: subprocess.Popen


class Environment:
    """Synchronous on purpose: the TUI calls it from a thread, and exit cleanup calls it directly."""

    def __init__(self) -> None:
        self.services: dict[str, Service] = {}
        self.lock = threading.RLock()         # one change at a time; a forced teardown skips it

    # --- processes ---

    def _spawn(self, name: str, argv: list[str], *, cwd: Path, pythonpath: Path | None = None) -> Service:
        LOGS.mkdir(parents=True, exist_ok=True)
        PIDS.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env[MARKER] = str(RUN)
        env["PYTHONUNBUFFERED"] = "1"
        if pythonpath is not None:
            env["PYTHONPATH"] = str(pythonpath)
        log = open(LOGS / f"{name}.log", "ab")
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        (PIDS / f"{name}.pid").write_text(f"{proc.pid}\n")
        service = Service(name, proc)
        self.services[name] = service
        return service

    def _stop(self, name: str, report: Report, sig: signal.Signals = signal.SIGTERM,
              grace: float = 8.0) -> None:
        pidfile = PIDS / f"{name}.pid"
        service = self.services.pop(name, None)
        pid = service.proc.pid if service else (int(pidfile.read_text()) if pidfile.exists() else None)
        if pid is None:
            return
        if kill_group(pid, sig) and wait_gone([pid], grace):
            kill_group(pid, signal.SIGKILL)
            wait_gone([pid], 3)
            report(f"killed {name} (pid {pid}) after {grace:.0f}s")
        else:
            report(f"stopped {name} (pid {pid})")
        if service:
            service.proc.wait(timeout=3)
        pidfile.unlink(missing_ok=True)

    # --- base state ---

    def check_ports(self) -> None:
        busy = [f"{port}: {holder}" for port in ALL_PORTS if (holder := port_holder(port))]
        if busy:
            raise EnvError("ports devenv needs are in use:\n  " + "\n  ".join(busy))

    def start_base(self, report: Report = _quiet) -> None:
        """PostgreSQL, the fake upstream, the file server and the daemon (job_1 ACTIVE, no agents)."""
        with self.lock:
            self._start_base(report)

    def _start_base(self, report: Report) -> None:
        self.check_ports()
        RUN.mkdir(parents=True, exist_ok=True)
        self._make_elastic_cert()

        report("starting postgres")
        data = RUN / "postgres" / "data"
        data.parent.mkdir(parents=True)
        subprocess.run([pg_bin() / "initdb", "-D", data, "-U", "cm", "--auth=trust", "-E", "UTF8"],
                       check=True, capture_output=True, env={**os.environ, MARKER: str(RUN)})
        pg = self._spawn("postgres", [str(pg_bin() / "postgres"), "-D", str(data),
                                      "-p", str(POSTGRES_PORT), "-c", "listen_addresses=127.0.0.1",
                                      "-c", "unix_socket_directories=", "-c", "fsync=off"],
                         cwd=RUN)
        wait_for("postgres", lambda: port_open(POSTGRES_PORT), 30, pg.proc, LOGS / "postgres.log")
        wait_for("postgres", lambda: subprocess.run(
            [pg_bin() / "createdb", "-h", "127.0.0.1", "-p", str(POSTGRES_PORT), "-U", "cm",
             "cluster_manager"], capture_output=True).returncode == 0, 30, pg.proc, LOGS / "postgres.log")

        report("starting the fake upstream")
        up = self._spawn("upstream", [PYTHON, str(HARNESS / "fake_upstream.py"), "--port", str(UPSTREAM_PORT)],
                         cwd=RUN)
        wait_for("fake upstream", lambda: port_open(UPSTREAM_PORT), 15, up.proc, LOGS / "upstream.log")

        report("starting the file server")
        src = RUN / "file-server" / "src"
        copy_source("file-server", src)
        write_env("file-server", src, FILE_SERVER_ENV)
        fs = self._spawn("file-server", [PYTHON, "-c", "from file_server.file_server import main; main()"],
                         cwd=src, pythonpath=src)
        wait_for("file server", lambda: port_open(FILE_SERVER_PORT), 30, fs.proc, LOGS / "file-server.log")

        report("starting the daemon (bootstraps job_1)")
        src = RUN / "cluster-manager" / "src"
        copy_source("cluster-manager", src)
        write_env("cluster-manager", src, DAEMON_ENV)
        daemon = self._spawn("daemon", [PYTHON, "-m", "cluster_manager.daemon"], cwd=src, pythonpath=src)
        wait_for("daemon", lambda: httpx.get(f"{DAEMON_URL}/status", timeout=2).is_success,
                 60, daemon.proc, LOGS / "daemon.log")
        report("base ready: job_1 ACTIVE, no agents")

    def _make_elastic_cert(self) -> None:
        folder = RUN / "elastic"
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-subj", "/CN=devenv-elastic", "-keyout", folder / "elastic.key",
                        "-out", folder / "elastic.cert"], check=True, capture_output=True)

    # --- agents ---

    def start_agent(self, n: int, report: Report = _quiet) -> None:
        with self.lock:
            self._start_agent(n, report)

    def _start_agent(self, n: int, report: Report) -> None:
        home = RUN / f"agent{n}"
        src = home / "src"
        if not src.exists():
            copy_source("agent", src)
            for sub in ("state", "worker/console", "worker/etc", "worker/hypertree", "downloads", "bundles"):
                (home / sub).mkdir(parents=True, exist_ok=True)
            write_env("agent", src, agent_env(n))
        report(f"starting agent {n} ({AGENT_IPS[n - 1]})")
        proc = self._spawn(f"agent{n}", [PYTHON, "-m", "agent"], cwd=src, pythonpath=src)
        wait_for(f"agent {n}", lambda: port_open(AGENT_PORTS[n - 1]), 30, proc.proc, LOGS / f"agent{n}.log")

    def stop_agent(self, n: int, report: Report = _quiet) -> None:
        """SIGTERM, as systemd stops it. The worker keeps running (its own process group)."""
        with self.lock:
            self._stop(f"agent{n}", report, signal.SIGTERM, grace=45)

    def worker_pid(self, n: int) -> int | None:
        pidfile = PIDS / f"worker-{WORKER_PORTS[n - 1]}.pid"
        try:
            pid = int(pidfile.read_text())
        except (OSError, ValueError):
            return None
        return pid if psutil.pid_exists(pid) else None

    def worker_control(self, n: int, **settings: object) -> dict:
        query = {k: (1 if v is True else 0 if v is False else v) for k, v in settings.items()}
        response = httpx.get(f"http://127.0.0.1:{WORKER_PORTS[n - 1]}/control", params=query, timeout=5)
        response.raise_for_status()
        return response.json()

    # --- cm ---

    def cm_argv(self, *args: str) -> list[str]:
        return [PYTHON, "-c", "from cluster_manager.client.cli import app; app(prog_name='cm')",
                "--daemon-url", DAEMON_URL, *args]

    def cm_env(self, color: bool = False, width: int = 100) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "NO_COLOR", "CM_DAEMON_URL")}
        env["PYTHONPATH"] = str(RUN / "cluster-manager" / "src")
        env["COLUMNS"] = str(width)
        if color:
            env["FORCE_COLOR"] = "1"
            env["TTY_COMPATIBLE"] = "1"
        return env

    def is_up(self) -> bool:
        return "daemon" in self.services and self.services["daemon"].proc.poll() is None

    # --- teardown ---

    def teardown(self, report: Report = _quiet, *, force: bool = False) -> None:
        """Everything, in order: agents, workers, daemon, file server, upstream, postgres, then
        any process still carrying our marker, then .run/. Safe to call again.
        force: SIGKILL everything at once, without waiting for a teardown already running."""
        if force:
            for pidfile in PIDS.glob("*.pid") if PIDS.exists() else []:
                try:
                    kill_group(int(pidfile.read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
            for line in sweep():
                report(line)
            shutil.rmtree(RUN, ignore_errors=True)
            return
        with self.lock:
            self._teardown(report)

    def _teardown(self, report: Report) -> None:
        for name in ("agent1", "agent2"):
            self._stop(name, report, signal.SIGTERM, grace=10)
        for pidfile in sorted(PIDS.glob("worker-*.pid")) if PIDS.exists() else []:
            try:
                pid = int(pidfile.read_text())
            except (OSError, ValueError):
                pid = None
            if pid and psutil.pid_exists(pid) and is_ours(psutil.Process(pid)):
                kill_group(pid, signal.SIGTERM)
                if wait_gone([pid], 3):
                    kill_group(pid, signal.SIGKILL)
                    wait_gone([pid], 2)
                report(f"stopped worker {pidfile.stem.removeprefix('worker-')} (pid {pid})")
            pidfile.unlink(missing_ok=True)
        for name in ("daemon", "file-server", "upstream"):
            self._stop(name, report, signal.SIGTERM, grace=8)
        self._stop("postgres", report, signal.SIGINT, grace=10)    # fast shutdown
        self.services.clear()
        strays = sweep()
        for line in strays:
            report(line)
        if RUN.exists():
            shutil.rmtree(RUN, ignore_errors=True)
            report("removed devenv/.run")


def sweep() -> list[str]:
    """Kills every process that carries our marker: leftovers from a run killed with kill -9,
    or workers no pidfile names any more. Returns what it killed."""
    found = []
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.pid != me and is_ours(proc):
            found.append(proc)
    for proc in found:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(found, timeout=3)
    return [f"killed stray {p.info['name']} (pid {p.pid})" for p in found]


def clear_leftovers(report: Report = _quiet) -> None:
    """At startup: a previous run that never cleaned up (kill -9, power loss)."""
    if not RUN.exists():
        return
    report("clearing leftovers from an earlier run")
    for pidfile in PIDS.glob("*.pid") if PIDS.exists() else []:
        try:
            pid = int(pidfile.read_text())
            if psutil.pid_exists(pid) and is_ours(psutil.Process(pid)):
                kill_group(pid, signal.SIGKILL)
                report(f"killed leftover {pidfile.stem} (pid {pid})")
        except (OSError, ValueError, psutil.Error):
            pass
    for line in sweep():
        report(line)
    shutil.rmtree(RUN, ignore_errors=True)
