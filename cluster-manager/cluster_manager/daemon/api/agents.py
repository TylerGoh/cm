"""Per-agent actions for the client. Each changes the desired state and answers with it."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.session import get_session
from cluster_manager.daemon.services import desired_state
from cluster_manager.daemon.services.errors import ActionRefused, AgentNotFound
from cluster_manager.schemas import DecommissionResponse, DesiredStateChangeResponse

router = APIRouter(prefix="/agents/{ip}")
Session = Annotated[AsyncSession, Depends(get_session)]


def _refused(ip: str, error: Exception) -> HTTPException:
    if isinstance(error, AgentNotFound):
        return HTTPException(status_code=404, detail=f"no agent {ip}")
    return HTTPException(status_code=409, detail=str(error))


@router.post("/restart", response_model=DesiredStateChangeResponse)
async def restart(session: Session, ip: str) -> DesiredStateChangeResponse:
    try:
        return await desired_state.restart(session, ip)
    except (AgentNotFound, ActionRefused) as e:
        raise _refused(ip, e)


@router.post("/force-update", response_model=DesiredStateChangeResponse)
async def force_update(session: Session, ip: str) -> DesiredStateChangeResponse:
    try:
        return await desired_state.force_update(session, ip)
    except (AgentNotFound, ActionRefused) as e:
        raise _refused(ip, e)


@router.post("/stop", response_model=DesiredStateChangeResponse)
async def stop(session: Session, ip: str) -> DesiredStateChangeResponse:
    try:
        return await desired_state.set_running(session, ip, running=False)
    except (AgentNotFound, ActionRefused) as e:
        raise _refused(ip, e)


@router.post("/start", response_model=DesiredStateChangeResponse)
async def start(session: Session, ip: str) -> DesiredStateChangeResponse:
    try:
        return await desired_state.set_running(session, ip, running=True)
    except (AgentNotFound, ActionRefused) as e:
        raise _refused(ip, e)


@router.post("/decommission", response_model=DecommissionResponse)
async def decommission(session: Session, ip: str) -> DecommissionResponse:
    try:
        return await desired_state.decommission(session, ip)
    except (AgentNotFound, ActionRefused) as e:
        raise _refused(ip, e)
