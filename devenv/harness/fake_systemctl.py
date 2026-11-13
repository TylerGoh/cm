"""A stand-in for systemctl, for the devenv agents (first on their PATH as .run/bin/systemctl).

Knows the units under .run/units/: <name>.json holds the command, <name>.state.json what this
script last did. Handles what the agent uses: show, start, stop [--no-block], kill
--signal=SIGKILL and reset-failed. Each unit behaves like the install script's: Restart=no,
KillSignal=SIGKILL. The worker runs in its own session, outliving the agent; its stdout and
stderr go to .run/logs/<name>.log in place of the journal.

A stop (always a SIGKILL) ends inactive; a worker that dies any other way ends failed, as with
Restart=no, until the next start or reset-failed.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

RUN = Path(__file__).resolve().parent.parent / ".run"
UNITS = RUN / "units"
LOGS = RUN / "logs"


def _load(name: str) -> dict:
    try:
        return json.loads((UNITS / f"{name}.state.json").read_text())
    except FileNotFoundError:
        return {"pid": None, "create_time": None}


def _save(name: str, state: dict) -> None:
    (UNITS / f"{name}.state.json").write_text(json.dumps(state))


def _process(state: dict) -> psutil.Process | None:
    if state["pid"] is None:
        return None
    try:
        proc = psutil.Process(state["pid"])
        if abs(proc.create_time() - state["create_time"]) > 1 or proc.status() == psutil.STATUS_ZOMBIE:
            return None
    except psutil.NoSuchProcess:
        return None
    return proc


def _kill(state: dict) -> None:
    proc = _process(state)
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while _process(state) is not None and time.monotonic() < deadline:
        time.sleep(0.05)


def show(name: str, properties: list[str]) -> None:
    loaded = (UNITS / f"{name}.json").exists()
    state = _load(name)
    if _process(state) is not None:
        active, result = "active", "success"
    elif state["pid"] is not None:
        active, result = "failed", "exit-code"           # died, and no stop cleared it
    else:
        active, result = "inactive", "success"
    values = {"LoadState": "loaded" if loaded else "not-found", "ActiveState": active,
              "Result": result, "Restart": "no", "KillSignal": "9"}
    for p in properties:
        print(f"{p}={values.get(p, '')}")


def start(name: str) -> None:
    unit_file = UNITS / f"{name}.json"
    if not unit_file.exists():
        sys.exit(f"Failed to start {name}: Unit {name} not found.")
    state = _load(name)
    if _process(state) is not None:
        return
    command = json.loads(unit_file.read_text())["command"]
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{name}.log", "ab") as journal:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=journal,
                                stderr=subprocess.STDOUT, start_new_session=True)
    _save(name, {"pid": proc.pid, "create_time": psutil.Process(proc.pid).create_time()})


def main(argv: list[str]) -> None:
    args = [a for a in argv if a not in ("--no-block", "--signal=SIGKILL")]
    verb, name = args[0], args[1]
    match verb:
        case "show":
            show(name, [args[i + 1] for i, a in enumerate(args) if a == "-p"])
        case "start":
            start(name)
        case "stop":
            _kill(_load(name))
            _save(name, {"pid": None, "create_time": None})
        case "kill":
            _kill(_load(name))                           # keeps the pid: shows failed
        case "reset-failed":
            if _process(_load(name)) is None:
                _save(name, {"pid": None, "create_time": None})
        case _:
            sys.exit(f"fake systemctl: {verb} isn't handled")


if __name__ == "__main__":
    main(sys.argv[1:])
