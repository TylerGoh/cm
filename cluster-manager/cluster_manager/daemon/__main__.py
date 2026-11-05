import logging
import time

import uvicorn

from cluster_manager.daemon.config import config

BIND_HOST = "0.0.0.0"            # every service listens on all interfaces


def main() -> None:
    """Runs the daemon in the foreground. `cm daemon` calls this, and systemd runs `cm daemon`."""
    logging.Formatter.converter = time.gmtime      # %(asctime)s in UTC, like every other timestamp
    logging.basicConfig(level=config.log_level, format=config.log_format)

    from cluster_manager.daemon.app import app     # after logging is set up

    # One process: the canary scheduler and the loops live in it, so no workers > 1.
    uvicorn.run(app, host=BIND_HOST, port=config.port, lifespan="on", log_config=None,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
