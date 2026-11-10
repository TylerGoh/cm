"""The client's only way into the daemon: one method per endpoint, each returning the
response model from cluster_manager/schemas.py."""

import httpx
from pydantic import BaseModel

from cluster_manager.schemas import (
    AgentCondition, AgentDetailResponse, AgentListResponse, CanaryResponse, DecommissionResponse,
    DesiredStateChangeResponse, DispatchRequest, DispatchResponse, IncidentListResponse,
    RollbackResponse, StatusResponse, VersionListResponse,
)


class DaemonError(Exception):
    """The daemon answered with an error. `detail` is its explanation."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class DaemonClient:
    def __init__(self, base_url: str, *, timeout: float = 660.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        # Long enough for dispatch, which waits for the file server's fetch from upstream.
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _check(self, response: httpx.Response) -> None:
        if response.is_error:
            try:
                detail = str(response.json()["detail"])
            except Exception:
                detail = response.text[:200]
            raise DaemonError(response.status_code, detail)

    def _get[T: BaseModel](self, path: str, model: type[T], params: dict[str, str] | None = None) -> T:
        response = self._http.get(path, params=params)
        self._check(response)
        return model.model_validate_json(response.content)

    def _post[T: BaseModel](self, path: str, model: type[T], body: BaseModel | None = None) -> T:
        content = body.model_dump_json() if body is not None else None
        response = self._http.post(path, content=content,
                                   headers={"Content-Type": "application/json"})
        self._check(response)
        return model.model_validate_json(response.content)

    # --- reads ---

    def status(self) -> StatusResponse:
        return self._get("/status", StatusResponse)

    def agents(self, *, condition: AgentCondition | None = None, version: str | None = None,
               problems: bool = False, canary: bool = False) -> AgentListResponse:
        params: dict[str, str] = {}
        if condition is not None:
            params["condition"] = condition
        if version is not None:
            params["version"] = version
        if problems:
            params["problems"] = "true"
        if canary:
            params["canary"] = "true"
        return self._get("/agents", AgentListResponse, params)

    def agent(self, ip: str) -> AgentDetailResponse:
        return self._get(f"/agents/{ip}", AgentDetailResponse)

    def versions(self) -> VersionListResponse:
        return self._get("/versions", VersionListResponse)

    def canary(self) -> CanaryResponse:
        return self._get("/canary", CanaryResponse)

    def incidents(self) -> IncidentListResponse:
        return self._get("/incidents", IncidentListResponse)

    # --- actions ---

    def restart(self, ip: str) -> DesiredStateChangeResponse:
        return self._post(f"/agents/{ip}/restart", DesiredStateChangeResponse)

    def force_update(self, ip: str) -> DesiredStateChangeResponse:
        return self._post(f"/agents/{ip}/force-update", DesiredStateChangeResponse)

    def stop(self, ip: str) -> DesiredStateChangeResponse:
        return self._post(f"/agents/{ip}/stop", DesiredStateChangeResponse)

    def start(self, ip: str) -> DesiredStateChangeResponse:
        return self._post(f"/agents/{ip}/start", DesiredStateChangeResponse)

    def decommission(self, ip: str) -> DecommissionResponse:
        return self._post(f"/agents/{ip}/decommission", DecommissionResponse)

    def dispatch(self, version: str, upstream_url: str) -> DispatchResponse:
        return self._post("/versions/dispatch", DispatchResponse,
                          DispatchRequest(version=version, upstream_url=upstream_url))

    def rollback(self) -> RollbackResponse:
        return self._post("/versions/rollback", RollbackResponse)
