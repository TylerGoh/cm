"""The version_deployments flows: the startup bootstrap, dispatch (new -> VERIFYING), the canary
judgement at its deadline (VERIFYING -> ACTIVE or UNSTABLE), and a requested rollback (ACTIVE ->
UNSTABLE, latest STABLE -> ACTIVE). Each flow is one transaction that keeps exactly one ACTIVE:
leaving ACTIVE always happens with another version becoming ACTIVE, demoting first and flushing,
since the one-ACTIVE index is checked per statement."""

import logging
from datetime import datetime, UTC

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.clients.file_server import FileServerClient
from cluster_manager.daemon.config import config
from cluster_manager.daemon.db.models.desired_state import DesiredStateRow
from cluster_manager.daemon.db.models.version_deployment import VersionDeployment
from cluster_manager.daemon.db.repositories.agent_repo import AgentRepo
from cluster_manager.daemon.db.repositories.desired_state_repo import DesiredStateRepo
from cluster_manager.daemon.db.repositories.observed_state_repo import ObservedStateRepo
from cluster_manager.daemon.db.repositories.version_deployment_repo import VersionDeploymentRepo
from cluster_manager.daemon.db.session import session_factory
from cluster_manager.daemon.services.conditions import canary_outcome, canary_rejected, count_outcomes
from cluster_manager.daemon.services.desired_state import history_row
from cluster_manager.daemon.services.errors import ActionRefused
from cluster_manager.daemon.services.views import canary_limits
from cluster_manager.schemas import (
    CanaryOutcome, DeploymentStatus, DispatchRequest, DispatchResponse, RollbackResponse,
)

log = logging.getLogger("cluster_manager.versions")


# --- bootstrap ---

async def ensure_bootstrap(
    session: AsyncSession,
    file_server: FileServerClient,
    bootstrap_version: str,
    bootstrap_upstream_url: str,
    now: datetime,
) -> VersionDeployment:
    """Runs once at startup, before anything reads the ACTIVE version.

    First boot (no version recorded): has the file server store the bootstrap artifact (an
    idempotent fetch, so one already there isn't fetched again) and seeds it as a verified ACTIVE,
    so it becomes STABLE, a rollback target, when the first canary is promoted.
    Any later boot: the config values aren't used; only checks that exactly one version is ACTIVE,
    and fails startup otherwise."""
    repo = VersionDeploymentRepo(session)
    if await repo.has_any():
        return await repo.active()                  # raises unless exactly one version is ACTIVE

    job = await file_server.fetch(bootstrap_upstream_url, bootstrap_version)
    seed = VersionDeployment(
        version=bootstrap_version,
        artifact_url=file_server.artifact_url(bootstrap_version),
        artifact_sha256=job.sha256,
        status=DeploymentStatus.ACTIVE,
        created_at=now,
        verified_at=now,
        activated_by_rollback=False,
        unstable_reason=None,
    )
    repo.add(seed)
    await session.commit()
    return seed


# --- dispatch ---

async def dispatch(session: AsyncSession, file_server: FileServerClient, scheduler: AsyncIOScheduler,
                   request: DispatchRequest) -> DispatchResponse:
    """Puts the version on the file server (waiting for the fetch), then starts its canary in one
    transaction: picks the sample, inserts the VERIFYING row, points the sample at it. With no
    eligible agent, nothing enters VERIFYING. A failed fetch raises before anything is written."""
    versions = VersionDeploymentRepo(session)
    if await versions.get(request.version) is not None:
        raise ActionRefused(f"{request.version} was already dispatched; a version is never deployed twice")
    current = await versions.verifying()
    if current is not None:
        raise ActionRefused(f"{current.version} is still being verified")

    job = await file_server.fetch(request.upstream_url, request.version)   # waits until stored

    now = datetime.now(UTC)
    sample = await ObservedStateRepo(session).canary_sample(config.canary_size, now, config.non_responding_after)
    if not sample:
        raise ActionRefused("no eligible agent for a canary (live and running)")
    versions.add(VersionDeployment(
        version=request.version,
        artifact_url=file_server.artifact_url(request.version),
        artifact_sha256=job.sha256,
        status=DeploymentStatus.VERIFYING,
        created_at=now,
        verified_at=None,
        activated_by_rollback=False,
        unstable_reason=None,
    ))
    await session.flush()                            # the version row before desired_state refers to it
    await _point(session, sample, request.version, now)
    await session.commit()

    deadline = now + config.canary_duration
    schedule_judgement(scheduler, request.version, deadline)
    log.info("dispatched %s to %d canary agents, judged at %s", request.version, len(sample), deadline)
    return DispatchResponse(version=request.version, canary_agents=sample, deadline=deadline)


# --- canary judgement ---

def schedule_judgement(scheduler: AsyncIOScheduler, version: str, deadline: datetime) -> None:
    """One job per canary, at its deadline. misfire_grace_time=None runs a late job (a deadline
    that passed while the daemon was down) as soon as the scheduler starts."""
    scheduler.add_job(judge_canary, "date", run_date=deadline, args=[version],
                      id=f"canary-{version}", replace_existing=True, misfire_grace_time=None)


async def reschedule_judgement(session: AsyncSession, scheduler: AsyncIOScheduler) -> None:
    """At startup: the job store is in memory, so a VERIFYING version's job is scheduled again.
    The deadline is derived from created_at, so nothing else needs storing."""
    verifying = await VersionDeploymentRepo(session).verifying()
    if verifying is not None:
        schedule_judgement(scheduler, verifying.version, verifying.created_at + config.canary_duration)


async def judge_canary(version: str) -> None:
    """Runs at the deadline. Only the state then counts. Promotes or rejects in one transaction."""
    async with session_factory() as session:
        versions = VersionDeploymentRepo(session)
        verifying = await versions.verifying()
        if verifying is None or verifying.version != version:
            log.warning("canary job for %s found it no longer VERIFYING; nothing to do", version)
            return
        now = datetime.now(UTC)
        rows = await AgentRepo(session).list_live(version)        # the canary agents
        counts = count_outcomes(canary_outcome(desired, observed) for _, desired, observed in rows)
        active = await versions.active()

        if canary_rejected(counts, canary_limits()):
            verifying.status = DeploymentStatus.UNSTABLE
            verifying.unstable_reason = "canary rejected: " + _counts_text(counts)
            await _point(session, [desired.ip for _, desired, _ in rows], active.version, now)
            await session.commit()
            log.warning("canary for %s rejected: %s", version, _counts_text(counts))
            return

        active.status = DeploymentStatus.STABLE
        await session.flush()                        # demote first: the one-ACTIVE index is per statement
        verifying.status = DeploymentStatus.ACTIVE
        verifying.verified_at = now
        verifying.activated_by_rollback = False
        await session.flush()
        fleet = [desired.ip for _, desired, _ in await AgentRepo(session).list_live()
                 if desired.version != version]
        await _point(session, fleet, version, now)
        await session.commit()
        log.info("canary for %s passed (%s); promoted to ACTIVE, %d agents follow",
                 version, _counts_text(counts), len(fleet))


# --- rollback ---

async def rollback(session: AsyncSession) -> RollbackResponse:
    """Requested by a human: ACTIVE -> UNSTABLE and the latest STABLE -> ACTIVE, and the main fleet
    follows. Never chains: an ACTIVE that came from a rollback isn't rolled back. Agents running a
    VERIFYING canary stay on it. What triggers a rollback automatically is not designed yet."""
    versions = VersionDeploymentRepo(session)
    active = await versions.active()
    if active.activated_by_rollback:
        raise ActionRefused(f"{active.version} became ACTIVE by a rollback; rollbacks don't chain")
    target = await versions.latest_stable()
    if target is None:
        raise ActionRefused("no STABLE version to roll back to")

    now = datetime.now(UTC)
    active.status = DeploymentStatus.UNSTABLE
    active.unstable_reason = f"rolled back by request at {now.isoformat()}"
    await session.flush()                            # demote first: the one-ACTIVE index is per statement
    target.status = DeploymentStatus.ACTIVE
    target.activated_by_rollback = True
    await session.flush()
    fleet = [desired.ip for _, desired, _ in await AgentRepo(session).list_live(active.version)]
    await _point(session, fleet, target.version, now)
    await session.commit()
    log.warning("rolled back %s -> %s on %d agents", active.version, target.version, len(fleet))
    return RollbackResponse(from_version=active.version, to_version=target.version, agents=len(fleet))


# --- helpers ---

async def _point(session: AsyncSession, ips: list[str], version: str, now: datetime) -> list[DesiredStateRow]:
    """Points the agents at `version` in one statement and writes their history in one insert."""
    repo = DesiredStateRepo(session)
    rows = await repo.point_at_version(ips, version, now)
    repo.add_history_many([history_row(row) for row in rows])
    return rows


def _counts_text(counts: dict[CanaryOutcome, int]) -> str:
    return ", ".join(f"{outcome} {n}" for outcome, n in counts.items())
