"""Read endpoints for the client: GET only, nothing here changes state."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.session import get_session
from cluster_manager.daemon.services import views
from cluster_manager.daemon.services.errors import AgentNotFound
from cluster_manager.schemas import (
    AgentCondition, AgentDetailResponse, AgentListResponse, CanaryResponse, IncidentListResponse,
    StatusResponse, VersionListResponse,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/status", response_model=StatusResponse)
async def status(session: Session) -> StatusResponse:
    return await views.status(session)


@router.get("/agents", response_model=AgentListResponse)
async def agents(session: Session, condition: AgentCondition | None = None,
                 version: str | None = None, problems: bool = False) -> AgentListResponse:
    return await views.agents(session, condition=condition, version=version, problems=problems)


@router.get("/agents/{ip}", response_model=AgentDetailResponse)
async def agent(session: Session, ip: str) -> AgentDetailResponse:
    try:
        return await views.agent_detail(session, ip)
    except AgentNotFound:
        raise HTTPException(status_code=404, detail=f"no agent {ip}")


@router.get("/versions", response_model=VersionListResponse)
async def versions(session: Session) -> VersionListResponse:
    return await views.versions(session)


@router.get("/canary", response_model=CanaryResponse)
async def canary(session: Session) -> CanaryResponse:
    return await views.canary(session)


@router.get("/incidents", response_model=IncidentListResponse)
async def incidents(session: Session) -> IncidentListResponse:
    return await views.incidents(session)
