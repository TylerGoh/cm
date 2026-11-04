"""The agents' only route: the report in, the bare desired state out (no envelope)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.session import get_session
from cluster_manager.daemon.services import heartbeat
from cluster_manager.daemon.wire import DesiredState, HeartbeatReport

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/heartbeat", response_model=DesiredState)
async def receive(session: Session, request: Request, report: HeartbeatReport) -> DesiredState:
    try:
        return await heartbeat.handle(session, report, request.app.state.file_server, datetime.now(UTC))
    except heartbeat.AgentDecommissioned:
        raise HTTPException(status_code=403, detail=f"{report.ipv4} is decommissioned")
