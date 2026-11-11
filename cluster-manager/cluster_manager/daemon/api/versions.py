"""Version requests for the client: dispatch (version_dispatch, also served as signature_dispatch)
and rollback."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.clients.file_server import FileServerError
from cluster_manager.daemon.db.session import get_session
from cluster_manager.daemon.services import versions
from cluster_manager.daemon.services.errors import ActionRefused
from cluster_manager.schemas import DispatchRequest, DispatchResponse, RollbackResponse

router = APIRouter(prefix="/versions")
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/dispatch", response_model=DispatchResponse)
@router.post("/signature_dispatch", response_model=DispatchResponse)   # an alias: the same request
async def version_dispatch(session: Session, request: Request, body: DispatchRequest) -> DispatchResponse:
    try:
        return await versions.dispatch(session, request.app.state.file_server,
                                       request.app.state.scheduler, body)
    except ActionRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (FileServerError, httpx.HTTPError) as e:
        raise HTTPException(status_code=502, detail=f"file server: {e}")


@router.post("/rollback", response_model=RollbackResponse)
async def rollback(session: Session) -> RollbackResponse:
    try:
        return await versions.rollback(session, reason="rolled back by request", expected_active=None)
    except ActionRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
