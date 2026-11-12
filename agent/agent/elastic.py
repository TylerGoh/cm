import logging
from pathlib import Path

from elasticsearch import (
    ApiError,
    AsyncElasticsearch,
    ConnectionError,
    ConnectionTimeout,
    SerializationError,
    SSLError,
    UnsupportedProductError,
)

from agent.models import HostInfo

log = logging.getLogger("agent.elastic")

# Shown after Elastic's own reason for the statuses that are almost always configuration.
HINTS = {
    401: "check ELASTIC_API_KEY (wrong, expired or revoked)",
    403: "the API key may not write to this index",
    404: "the index doesn't exist and Elastic won't create it",
}


class ElasticClient:
    """Never raises errors; it logs problems instead. A failed write isn't retried."""

    def __init__(self, base_url: str, host: HostInfo, *, cert_path: Path, api_key: str,
                 metrics_index: str, host_index: str, timeout: float,
                 client: AsyncElasticsearch | None = None) -> None:
        if not cert_path.is_file():                  # a configuration error: fails startup
            raise FileNotFoundError(f"elastic CA certificate missing: {cert_path}")
        # api_key is Elastic's encoded key (base64 of "id:key"), sent as-is on every request.
        self._es = client or AsyncElasticsearch(
            base_url, ca_certs=str(cert_path), api_key=api_key, request_timeout=timeout,
            max_retries=0, retry_on_timeout=False, node_class="httpxasync")
        self._base_url = base_url
        self._timeout = timeout
        self._host = host
        self._metrics_index = metrics_index
        self._host_index = host_index
        self._failure: dict[str, str] = {}           # what failed -> the failure last logged for it

    def host_doc_id(self, key: str) -> str:
        return f"{self._host.ipv4}_{key}"

    async def put_host(self, key: str, doc: dict[str, object]) -> None:
        """Writes the host document <ip>_<key>, replacing the one there."""
        await self._index(self._host_index, doc, doc_id=self.host_doc_id(key))

    async def delete_other_host_docs(self, keep: list[str]) -> None:
        """Deletes this host's host-index documents except the keys in keep and events. Matches
        the host on ip, and on ipv4 for documents written before the field was renamed."""
        ip = str(self._host.ipv4)
        query = {"bool": {
            "should": [{"term": {field: ip}} for field in ("ip", "ip.keyword", "ipv4", "ipv4.keyword")],
            "minimum_should_match": 1,
            "must_not": [{"ids": {"values": [self.host_doc_id(k) for k in keep]}},
                         {"exists": {"field": "event"}}],
        }}
        what = f"{self._host_index} (deleting old documents)"
        try:
            await self._es.delete_by_query(index=self._host_index, query=query, conflicts="proceed",
                                           ignore_unavailable=True, allow_no_indices=True)
        except Exception as exc:
            self._failed(what, exc)
            return
        self._worked(what)

    async def index_metrics(self, doc: dict[str, object]) -> None:
        await self._index(self._metrics_index, doc)          # the metrics exporter's documents

    async def aclose(self) -> None:
        await self._es.close()

    async def _index(self, index: str, doc: dict[str, object], doc_id: str | None = None) -> None:
        body = dict(doc)
        body["hostname"] = self._host.hostname
        body["ip"] = str(self._host.ipv4)
        body["cluster_name"] = self._host.cluster_name
        try:
            await self._es.index(index=index, document=body, id=doc_id)
        except Exception as exc:
            self._failed(index, exc)
            return
        self._worked(index)

    def _failed(self, what: str, exc: Exception) -> None:
        failure = self.describe(exc)
        if self._failure.get(what) != failure:
            log.warning("elastic write to %s failed: %s", what, failure)
            self._failure[what] = failure

    def _worked(self, what: str) -> None:
        if self._failure.pop(what, None) is not None:
            log.info("elastic writes to %s recovered", what)

    def describe(self, exc: Exception) -> str:
        """One line saying what went wrong, without the document or the key."""
        cause = exc.__cause__ or exc.__context__
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None and str(cause) else ""
        if isinstance(exc, UnsupportedProductError):
            return f"{self._base_url} isn't Elasticsearch, or its version doesn't match this client"
        if isinstance(exc, ApiError):
            error = exc.body.get("error") if isinstance(exc.body, dict) else None
            if isinstance(error, dict):
                text = f"{error.get('type', 'error')}: {error.get('reason', '')}"
            else:
                text = str(error if error is not None else exc.body)
            line = f"HTTP {exc.meta.status} {text[:300]}"
            hint = HINTS.get(exc.meta.status)
            return f"{line}; {hint}" if hint else line
        if isinstance(exc, SSLError):
            return f"TLS to {self._base_url} failed; check ELASTIC_CERT_PATH{detail}"
        if isinstance(exc, ConnectionTimeout):
            return f"no answer from {self._base_url} within {self._timeout}s"
        if isinstance(exc, ConnectionError):
            return f"can't connect to {self._base_url}{detail}"
        if isinstance(exc, SerializationError):
            return f"the document isn't JSON: {exc}"
        return f"{type(exc).__name__}: {exc}"
