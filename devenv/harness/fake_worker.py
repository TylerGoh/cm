"""A fake worker: a real process the agent spawns, that answers the worker contract.

    GET /api?command=show_version     [["type","version","active"],["app",<loaded version>,"Y"]]
    GET /api?command=counters         {"requests.out.total": .., "requests.old_failed.total": ..,
                                       "requests.out_lagged.total": ..}, cumulative
    GET /api?command=reload_phase1    reads the version the agent linked under etc/ (slow)
    GET /api?command=reload_phase2    switches to it and generates a hypertree (slow)
    GET /control?...                  for the scenarios, never the agent:
        bad=1|0           bad numbers: most requests fail, so the agent reports a violation
        hang=1|0          show_version and counters stall (past the agent's WORKER_TIMEOUT)
        crash=1           exits at once, as a crash would
        delay_<command>=<seconds>, delay_startup=<seconds>   capped at MAX_DELAY
    GET /control                      the current state, as JSON

A version with "bad" in its name always gives bad numbers once loaded. It starts with whatever
etc/ links to (a default install with no links reports "factory"), logs the ready line after a
startup delay, and writes its pid to <pids>/worker-<port>.pid (removed on a clean exit).
"""
import argparse
import hashlib
import json
import os
import random
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_DELAY = 30.0
READY_LINE = "worker ready"             # the agents' WORKER_INITIALIZE_FLAG
VERSION_FILE = Path("app/version.txt")  # under etc/: the link the agent makes to the bundle
FACTORY_VERSION = "factory"
REQUESTS_PER_SECOND = 200.0
GOOD_FAILED_RATIO = 0.02
BAD_FAILED_RATIO = 0.65


class Worker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.loaded_version = self._linked_version()
        self.staged_version: str | None = None
        self.bad = False
        self.hang = False
        self.delays = {                     # (min, max) seconds; a toggle sets both to one value
            "startup": (3.0, 8.0),
            "reload_phase1": (5.0, 15.0),
            "reload_phase2": (5.0, 15.0),
            "show_version": (0.0, 0.5),
            "counters": (0.0, 0.5),
        }
        self.out = 0.0
        self.old_failed = 0.0
        self.lagged = 0.0
        self.last = time.monotonic()

    # --- state ---

    def _linked_version(self) -> str:
        try:
            return (self.args.etc / VERSION_FILE).read_text().strip() or FACTORY_VERSION
        except OSError:
            return FACTORY_VERSION

    def is_bad(self) -> bool:
        return self.bad or "bad" in self.loaded_version

    def delay(self, name: str) -> None:
        low, high = self.delays.get(name, (0.0, 0.0))
        time.sleep(min(random.uniform(low, high), MAX_DELAY))

    def counters(self) -> dict[str, float]:
        with self.lock:
            now = time.monotonic()
            intake = (now - self.last) * REQUESTS_PER_SECOND
            self.last = now
            failed = intake * (BAD_FAILED_RATIO if self.is_bad() else GOOD_FAILED_RATIO)
            self.out += intake - failed
            self.old_failed += failed * 0.6
            self.lagged += failed * 0.4
            return {"requests.out.total": round(self.out),
                    "requests.old_failed.total": round(self.old_failed),
                    "requests.out_lagged.total": round(self.lagged)}

    def make_hypertree(self) -> None:
        """Loading a version generates its hypertree: one folder named by a hash."""
        root = self.args.hypertree
        root.mkdir(parents=True, exist_ok=True)
        if any(entry.is_dir() for entry in root.iterdir()):
            return                          # fetched from the file server, or already generated
        folder = root / hashlib.sha256(self.loaded_version.encode()).hexdigest()[:16] / "tier1"
        folder.mkdir(parents=True)
        (folder / "routes.bin").write_bytes(os.urandom(256))

    def log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.args.log, "a") as f:
            f.write(f"{stamp} pid={os.getpid()} {message}\n")


def handler_for(worker: Worker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            pass                            # quiet: the console log is the agent's to read

        def send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/control":
                self.run_control(query)
            elif url.path == "/api":
                self.run_command(query.get("command", ""))
            else:
                self.send(404, {"error": f"no such path {url.path}"})

        def run_command(self, name: str) -> None:
            if name in ("show_version", "counters") and worker.hang:
                time.sleep(MAX_DELAY)
            worker.delay(name)
            if name == "show_version":
                self.send(200, [["type", "version", "active"],
                                ["app", worker.loaded_version, "Y"]])
            elif name == "counters":
                self.send(200, worker.counters())
            elif name == "reload_phase1":
                worker.staged_version = worker._linked_version()
                worker.log(f"reload phase 1: staged {worker.staged_version}")
                self.send(200, {"ok": True})
            elif name == "reload_phase2":
                if worker.staged_version is None:
                    self.send(409, {"error": "reload_phase2 before reload_phase1"})
                    return
                worker.loaded_version, worker.staged_version = worker.staged_version, None
                worker.make_hypertree()
                worker.log(f"reload phase 2: running {worker.loaded_version}")
                self.send(200, {"ok": True})
            else:
                self.send(404, {"error": f"unknown command {name!r}"})

        def run_control(self, query: dict[str, str]) -> None:
            for key, value in query.items():
                if key == "bad":
                    worker.bad = value == "1"
                elif key == "hang":
                    worker.hang = value == "1"
                elif key.startswith("delay_"):
                    seconds = min(max(float(value), 0.0), MAX_DELAY)
                    worker.delays[key.removeprefix("delay_")] = (seconds, seconds)
                elif key != "crash":
                    self.send(400, {"error": f"unknown control {key!r}"})
                    return
            state = {"pid": os.getpid(), "version": worker.loaded_version, "bad": worker.is_bad(),
                     "hang": worker.hang, "delays": worker.delays}
            self.send(200, state)
            if query.get("crash") == "1":
                worker.log("crashing on request")
                self.wfile.flush()
                os._exit(70)                # no cleanup, like a real crash: the pidfile stays

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)          # WORKER_PROGRAM_LOG_PATH
    parser.add_argument("--etc", type=Path, required=True)          # ETC_DIR
    parser.add_argument("--hypertree", type=Path, required=True)    # HYPERTREE_DIR
    parser.add_argument("--pids", type=Path, required=True)
    args = parser.parse_args()

    worker = Worker(args)
    pidfile = args.pids / f"worker-{args.port}.pid"
    args.pids.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{os.getpid()}\n")

    def leave(*_: object) -> None:
        if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
            pidfile.unlink()
        os._exit(0)

    signal.signal(signal.SIGTERM, leave)
    signal.signal(signal.SIGINT, leave)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(worker))
    server.daemon_threads = True
    worker.log(f"starting with {worker.loaded_version} on port {args.port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()   # pings answer at once

    worker.delay("startup")
    worker.make_hypertree()
    worker.log(f"{READY_LINE}: running {worker.loaded_version}")
    print(f"{READY_LINE}: {worker.loaded_version}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
