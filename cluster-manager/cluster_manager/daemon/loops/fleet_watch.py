"""Judges the ACTIVE fleet every tick, for as long as a version is ACTIVE, and rolls back to the
latest STABLE when the fleet stays over a limit for config.fleet_breach_duration.

The fleet is judged by the canary's rules with higher limits (services/conditions.fleet_outcome
and fleet_failing). A breach must last: a crash is followed within a tick by the agent's own
restart, so a single over-limit reading is not enough. Only an ACTIVE that can be rolled back is
watched: one that came from a rollback, or one with no STABLE behind it, is skipped.

The breach is held in memory: a daemon restart starts it again, which can only delay a rollback.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.session import session_factory
from cluster_manager.daemon.services import versions
from cluster_manager.daemon.services.errors import ActionRefused
from cluster_manager.schemas import CanaryOutcome

log = logging.getLogger("cluster_manager.fleet_watch")

FLEET_WATCH_INTERVAL = 10.0         # one agent tick


@dataclass(frozen=True, slots=True)
class Breach:
    """The ACTIVE version has been over a limit since `since`."""
    version: str
    since: datetime


async def run() -> None:
    breach: Breach | None = None
    while True:
        try:
            breach = await _step(breach)
        except Exception:
            log.exception("fleet watch step failed")
        await asyncio.sleep(FLEET_WATCH_INTERVAL)


async def _step(breach: Breach | None) -> Breach | None:
    """One judgement. Returns the breach still running, or None."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        verdict = await versions.fleet_verdict(session, now)
        if verdict is None:
            return None                                    # nothing a failure could do
        if breach is not None and breach.version != verdict.version:
            breach = None                                  # the ACTIVE changed: start over

        if not verdict.failing:
            if breach is not None:
                log.info("fleet on %s is back within its limits (%s)", verdict.version, _counts_text(verdict.counts))
            return None

        if breach is None:
            breach = Breach(version=verdict.version, since=now)
            log.warning("fleet on %s is over its limits (%s); rolling back if it lasts %ss",
                        verdict.version, _counts_text(verdict.counts), config.fleet_breach_seconds)
        if now - breach.since < config.fleet_breach_duration:
            return breach

        reason = f"rolled back automatically: fleet over its limits ({_counts_text(verdict.counts)})"
        try:
            result = await versions.rollback(session, reason=reason, expected_active=verdict.version)
        except ActionRefused as refusal:
            log.warning("fleet on %s failed, but no rollback: %s", verdict.version, refusal)
            return None
        log.warning("rolled back %s -> %s automatically on %d agents",
                    result.from_version, result.to_version, result.agents)
        return None


def _counts_text(counts: dict[CanaryOutcome, int]) -> str:
    return ", ".join(f"{outcome} {n}" for outcome, n in counts.items() if n) or "nothing to count"
