import httpx

from agent.wire import DesiredState, HeartbeatReport

HEARTBEAT_TIMEOUT = 5.0             # part of the ~40s systemd TimeoutStopSec budget
HEARTBEAT_PATH = "/heartbeat"


class ClusterManagerClient:
    """The heartbeat: the report out, the full desired state back. Raises on any error, including
    a refused heartbeat (a decommissioned agent); the supervisor treats every error the same.
    There is no register call: the first heartbeat from an unknown IP registers the agent."""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=HEARTBEAT_TIMEOUT, transport=transport)

    async def heartbeat(self, report: HeartbeatReport) -> DesiredState:
        response = await self._http.post(
            HEARTBEAT_PATH,
            content=report.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return DesiredState.model_validate_json(response.content)    # the body is the desired state itself

    async def aclose(self) -> None:
        await self._http.aclose()
