import asyncio
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
from elasticsearch import (
    ApiError,
    AuthenticationException,
    BadRequestError,
    ConnectionError,
    ConnectionTimeout,
    SSLError,
)

from agent.elastic import ElasticClient
from agent.models import HostInfo

URL = "https://elastic:9200"


def api_error(cls: type[ApiError], status: int, body: object) -> ApiError:
    meta = ApiResponseMeta(status=status, http_version="1.1", headers=HttpHeaders(), duration=0.0,
                           node=NodeConfig("https", "elastic", 9200))
    return cls(message="error", meta=meta, body=body)


def caused(exc: Exception, cause: Exception) -> Exception:
    exc.__cause__ = cause
    return exc


class FakeEs:
    """Raises the queued exceptions in order, then succeeds; records what was sent."""
    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.sent: list[tuple] = []
        self.deletes: list[dict[str, object]] = []

    async def index(self, *, index: str, document: dict[str, object], id: str | None = None) -> None:
        self.sent.append((index, document) if id is None else (index, id, document))
        if self.failures:
            raise self.failures.pop(0)

    async def delete_by_query(self, **kwargs: object) -> None:
        self.deletes.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)

    async def close(self) -> None:
        pass


def client(tmp_path: Path, es: FakeEs) -> ElasticClient:
    cert = tmp_path / "elastic.cert"
    cert.write_text("cert")
    host = HostInfo(hostname="box1", ipv4=IPv4Address("10.0.0.1"), cluster_name="sg-prod")
    return ElasticClient(URL, host, cert_path=cert, api_key="key", metrics_index="m", host_index="h",
                         timeout=2, client=es)  # type: ignore[arg-type]


def test_documents_get_the_host_and_go_to_their_index(tmp_path: Path) -> None:
    es = FakeEs()
    c = client(tmp_path, es)
    asyncio.run(c.index_metrics({"a": 1}))
    asyncio.run(c.put_host("state", {"b": 2}))
    host = {"hostname": "box1", "ip": "10.0.0.1", "cluster_name": "sg-prod"}
    assert es.sent == [("m", {"a": 1, **host}), ("h", "10.0.0.1_state", {"b": 2, **host})]


def test_host_documents_have_a_fixed_id_per_ip(tmp_path: Path) -> None:
    es = FakeEs()
    asyncio.run(client(tmp_path, es).put_host("intf_eth0", {"a": 1}))
    assert es.sent == [("h", "10.0.0.1_intf_eth0", {"a": 1, "hostname": "box1", "ip": "10.0.0.1",
                                                    "cluster_name": "sg-prod"})]


def test_deleting_old_documents_keeps_the_current_ones_and_events(tmp_path: Path) -> None:
    es = FakeEs()
    asyncio.run(client(tmp_path, es).delete_other_host_docs(keep=["intf_eth0", "state"]))
    [call] = es.deletes
    assert call["index"] == "h"
    query = call["query"]["bool"]                                   # type: ignore[index]
    assert {"term": {"ip": "10.0.0.1"}} in query["should"]
    assert {"term": {"ipv4": "10.0.0.1"}} in query["should"]        # documents from before the rename
    assert query["minimum_should_match"] == 1
    assert {"ids": {"values": ["10.0.0.1_intf_eth0", "10.0.0.1_state"]}} in query["must_not"]
    assert {"exists": {"field": "event"}} in query["must_not"]      # agent_started stays


def test_a_failed_delete_is_logged_and_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    denied = api_error(ApiError, 403, {"error": {"type": "security_exception", "reason": "action [indices:data/write/delete/byquery] is unauthorized"}})
    with caplog.at_level("WARNING", logger="agent.elastic"):
        asyncio.run(client(tmp_path, FakeEs(denied)).delete_other_host_docs(keep=["state"]))
    [record] = caplog.records
    assert "deleting old documents" in record.getMessage() and "HTTP 403" in record.getMessage()


def test_a_missing_cert_fails_startup(tmp_path: Path) -> None:
    host = HostInfo(hostname="box1", ipv4=IPv4Address("10.0.0.1"), cluster_name="sg-prod")
    with pytest.raises(FileNotFoundError, match="elastic CA certificate"):
        ElasticClient(URL, host, cert_path=tmp_path / "missing.cert", api_key="key",
                      metrics_index="m", host_index="h", timeout=2)


@pytest.mark.parametrize("exc, expected", [
    (api_error(BadRequestError, 400, {"error": {"type": "document_parsing_exception",
                                                 "reason": "failed to parse field [uptime] of type [float]"}}),
     "HTTP 400 document_parsing_exception: failed to parse field [uptime] of type [float]"),
    (api_error(AuthenticationException, 401, {"error": {"type": "security_exception", "reason": "unable to authenticate"}}),
     "HTTP 401 security_exception: unable to authenticate; check ELASTIC_API_KEY"),
    (api_error(ApiError, 502, "<html>bad gateway</html>"), "HTTP 502 <html>bad gateway</html>"),
    (caused(SSLError("TLS error"), OSError("certificate verify failed")),
     f"TLS to {URL} failed; check ELASTIC_CERT_PATH (OSError: certificate verify failed)"),
    (ConnectionTimeout("Connection timed out"), f"no answer from {URL} within 2s"),
    (caused(ConnectionError("Connection error"), OSError("All connection attempts failed")),
     f"can't connect to {URL} (OSError: All connection attempts failed)"),
])
def test_failures_say_what_went_wrong(tmp_path: Path, exc: Exception, expected: str,
                                      caplog: pytest.LogCaptureFixture) -> None:
    c = client(tmp_path, FakeEs(exc))
    with caplog.at_level("WARNING", logger="agent.elastic"):
        asyncio.run(c.index_metrics({"a": 1}))                  # never raises
    [record] = caplog.records
    assert record.getMessage().startswith(f"elastic write to m failed: {expected}")


def test_logged_again_only_when_the_failure_changes_then_recovery(tmp_path: Path,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    refused = ConnectionError("Connection error")
    c = client(tmp_path, FakeEs(refused, refused, ConnectionTimeout("t"), ConnectionTimeout("t")))
    with caplog.at_level("INFO", logger="agent.elastic"):
        for _ in range(5):
            asyncio.run(c.index_metrics({"a": 1}))
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", f"elastic write to m failed: can't connect to {URL}"),
        ("WARNING", f"elastic write to m failed: no answer from {URL} within 2s"),
        ("INFO", "elastic writes to m recovered"),
    ]


def test_each_index_logs_its_own_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    bad = api_error(BadRequestError, 400, {"error": {"type": "mapper_parsing_exception", "reason": "x"}})
    c = client(tmp_path, FakeEs(bad))
    with caplog.at_level("INFO", logger="agent.elastic"):
        asyncio.run(c.index_metrics({"a": 1}))                  # fails
        asyncio.run(c.put_host("state", {"b": 1}))              # works: no recovery line for h
        asyncio.run(c.index_metrics({"a": 1}))                  # works: m recovered
    assert [r.levelname for r in caplog.records] == ["WARNING", "INFO"]
