import logging
import ssl
from pathlib import Path

import httpx

from agent.models import HostInfo

log = logging.getLogger("agent.elastic")


class ElasticClient:
    """Best effort: never raises (cancellation still propagates), logs the first failure and the
    recovery. Two indexes; callers pick one by method and never see index names. Every document
    gets the host's identity added here, so callers never handle it. Authenticates with an API key
    and verifies Elastic's certificate with the CA in elastic.cert."""

    def __init__(self, base_url: str, host: HostInfo, *, cert_path: Path, api_key: str,
                 metrics_index: str, host_index: str, timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        ssl_context = ssl.create_default_context(cafile=cert_path)   # a missing cert fails startup
        # api_key is Elastic's encoded key (base64 of "id:key"), sent as-is on every request.
        self._client = httpx.AsyncClient(base_url=base_url, verify=ssl_context, timeout=timeout,
                                         headers={"Authorization": f"ApiKey {api_key}"},
                                         transport=transport)
        self._host = host
        self._metrics_index = metrics_index
        self._host_index = host_index
        self._failing = False

    async def index_host(self, doc: dict[str, object]) -> None:
        await self._index(self._host_index, doc)

    async def index_metrics(self, doc: dict[str, object]) -> None:
        await self._index(self._metrics_index, doc)          # the metrics exporter's documents

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _index(self, index: str, doc: dict[str, object]) -> None:
        body = dict(doc)
        body["hostname"] = self._host.hostname
        body["ipv4"] = str(self._host.ipv4)
        try:
            response = await self._client.post(f"/{index}/_doc", json=body)
            response.raise_for_status()
        except Exception as exc:
            if not self._failing:
                log.warning("elastic write to %s failed: %s", index, exc)
                self._failing = True
            return
        if self._failing:
            log.info("elastic writes recovered")
            self._failing = False
