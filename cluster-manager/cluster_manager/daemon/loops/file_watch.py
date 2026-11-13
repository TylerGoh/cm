"""Keeps every version agents may still be pointed at on the file server: each one that isn't
UNSTABLE is fetched every FILE_WATCH_INTERVAL, with the sha256 recorded at dispatch. A stored
version costs one request; one deleted by hand is fetched again from its upstream_url.

An artifact upstream now serves with another hash is not stored (the file server refuses it), and
opens an ARTIFACT_MISMATCH incident from the cluster manager for that version. A later fetch that
succeeds closes it. Any other failure is logged and tried again on the next tick.

No session is held during a fetch, which can take as long as FILE_SERVER_FETCH_TIMEOUT.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from cluster_manager.daemon.clients.file_server import ArtifactMismatch, FileServerClient
from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.repositories.incident_repo import IncidentRepo
from cluster_manager.daemon.db.repositories.version_deployment_repo import VersionDeploymentRepo
from cluster_manager.daemon.db.session import session_factory
from cluster_manager.schemas import IncidentKind, IncidentSource

log = logging.getLogger("cluster_manager.file_watch")

FILE_WATCH_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class Watched:
    version: str
    upstream_url: str
    artifact_sha256: str


async def run(file_server: FileServerClient) -> None:
    while True:
        try:
            await _step(file_server)
        except Exception:
            log.exception("file watch step failed")
        await asyncio.sleep(FILE_WATCH_INTERVAL)


async def _step(file_server: FileServerClient) -> None:
    async with session_factory() as session:
        watched = [Watched(row.version, row.upstream_url, row.artifact_sha256)
                   for row in await VersionDeploymentRepo(session).not_unstable()]
    for item in watched:
        try:
            await file_server.fetch(item.upstream_url, item.version, expected_sha256=item.artifact_sha256)
        except ArtifactMismatch as mismatch:
            await _mismatch(item.version, str(mismatch))
        except Exception as error:
            log.warning("file watch: fetching %s failed: %s", item.version, error)
        else:
            await _matched(item.version)


async def _mismatch(version: str, detail: str) -> None:
    async with session_factory() as session:
        opened = await IncidentRepo(session).open(
            config.ipv4, IncidentKind.ARTIFACT_MISMATCH, datetime.now(UTC),
            source=IncidentSource.CLUSTER_MANAGER, version=version,
        )
        await session.commit()
    if opened is not None:
        log.error("%s: %s", version, detail)


async def _matched(version: str) -> None:
    async with session_factory() as session:
        closed = await IncidentRepo(session).close_open(
            config.ipv4, IncidentKind.ARTIFACT_MISMATCH, datetime.now(UTC),
            source=IncidentSource.CLUSTER_MANAGER, version=version,
        )
        await session.commit()
    if closed is not None:
        log.info("%s: the file server has the artifact with the recorded sha256 again", version)
