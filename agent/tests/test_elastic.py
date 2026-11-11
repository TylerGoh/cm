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
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def index(self, *, index: str, document: dict[str, object]) -> None:
        self.sent.append((index, document))
        if self.failures:
            raise self.failures.pop(0)

    async def close(self) -> None:
        pass


def client(tmp_path: Path, es: FakeEs) -> ElasticClient:
    cert = tmp_path / "elastic.cert"
    cert.write_text("cert")
    host = HostInfo(hostname="box1", ipv4=IPv4Address("10.0.0.1"))
    return ElasticClient(URL, host, cert_path=cert, api_key="key", metrics_index="m", host_index="h",
                         timeout=2, client=es)  # type: ignore[arg-type]


def test_documents_get_the_host_and_go_to_their_index(tmp_path: Path) -> None:
    es = FakeEs()
    c = client(tmp_path, es)
    asyncio.run(c.index_metrics({"a": 1}))
    asyncio.run(c.index_host({"b": 2}))
    assert es.sent == [("m", {"a": 1, "hostname": "box1", "ipv4": "10.0.0.1"}),
                       ("h", {"b": 2, "hostname": "box1", "ipv4": "10.0.0.1"})]


def test_a_missing_cert_fails_startup(tmp_path: Path) -> None:
    host = HostInfo(hostname="box1", ipv4=IPv4Address("10.0.0.1"))
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
        asyncio.run(c.index_host({"b": 1}))                     # works: no recovery line for h
        asyncio.run(c.index_metrics({"a": 1}))                  # works: m recovered
    assert [r.levelname for r in caplog.records] == ["WARNING", "INFO"]
