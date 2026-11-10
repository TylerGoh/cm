"""./devenv/dev opens the TUI. One at a time: a second one would reset the first one's cluster."""
import os
import sys

import psutil

from harness.env import DEVENV, Environment
from harness.tui import DevenvApp, check_ready

LOCK = DEVENV / ".tui.pid"


def main() -> int:
    try:
        other = int(LOCK.read_text())
        if other != os.getpid() and psutil.pid_exists(other):
            print(f"devenv is already open (pid {other}); close that one first", file=sys.stderr)
            return 1
    except (OSError, ValueError):
        pass
    LOCK.write_text(f"{os.getpid()}\n")
    env = Environment()
    try:
        check_ready()
        DevenvApp(env).run()
    finally:
        env.teardown()                  # a no-op after a clean quit; catches a crash of the TUI
        LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
