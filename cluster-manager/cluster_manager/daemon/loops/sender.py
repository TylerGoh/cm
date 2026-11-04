"""Posts every incident opening and recovery to Mattermost, after commit and outside any
transaction, and marks each sent only once its post succeeded. An outage delays messages without
losing them, and an opening always goes out before its recovery. With no webhook configured,
nothing is sent and incidents stay pending."""

import asyncio
import logging
from datetime import UTC, datetime

from cluster_manager.daemon.clients.mattermost import MattermostClient
from cluster_manager.daemon.db.models.incident import Incident
from cluster_manager.daemon.db.repositories.incident_repo import IncidentRepo
from cluster_manager.daemon.db.session import session_factory

log = logging.getLogger("cluster_manager.sender")

SEND_INTERVAL = 10.0


async def run(mattermost: MattermostClient) -> None:
    failing = False
    while True:
        try:
            await _step(mattermost)
            if failing:
                log.info("mattermost posts recovered")
                failing = False
        except Exception:
            if not failing:
                log.exception("mattermost post failed; pending messages wait for the next try")
                failing = True
        await asyncio.sleep(SEND_INTERVAL)


async def _step(mattermost: MattermostClient) -> None:
    if not mattermost.enabled:
        return
    async with session_factory() as session:
        repo = IncidentRepo(session)
        openings = await repo.pending_openings()
        recoveries = await repo.pending_recoveries()
    for incident in openings:
        await mattermost.post(_opening_text(incident))
        await _mark(incident.id, recovery=False)
    for incident in recoveries:
        await mattermost.post(_recovery_text(incident))
        await _mark(incident.id, recovery=True)


async def _mark(incident_id: int, *, recovery: bool) -> None:
    async with session_factory() as session:
        repo = IncidentRepo(session)
        now = datetime.now(UTC)
        if recovery:
            await repo.mark_recovery_sent(incident_id, now)
        else:
            await repo.mark_opening_sent(incident_id, now)
        await session.commit()


def _opening_text(incident: Incident) -> str:
    return f":red_circle: {incident.agent_ip}: {incident.kind} since {incident.opened_at:%Y-%m-%d %H:%M:%S} UTC"


def _recovery_text(incident: Incident) -> str:
    assert incident.closed_at is not None
    return f":large_green_circle: {incident.agent_ip}: {incident.kind} recovered at {incident.closed_at:%Y-%m-%d %H:%M:%S} UTC"
