import logging
import time

import uvicorn

from config import config

BIND_HOST = "0.0.0.0"            # every service listens on all interfaces


def main() -> None:
    """The `agent` command, which systemd also runs."""
    # Logging first, before uvicorn starts, so it covers uvicorn's own startup lines and any error
    # before the lifespan runs. It stays out of app.py, so importing agent.app changes no logging.
    logging.Formatter.converter = time.gmtime      # %(asctime)s in UTC, like every other timestamp
    logging.basicConfig(level=config.log_level, format=config.log_format)

    from agent.app import app                      # after logging is set up

    # The app object, not an import string: no workers > 1 and no --reload, so exactly one supervisor.
    # lifespan="on": a lifespan error (a corrupt state file) stops uvicorn instead of serving without an agent.
    # log_config=None: uvicorn's records go through the root handler set up above.
    uvicorn.run(app, host=BIND_HOST, port=config.port, lifespan="on", log_config=None,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
