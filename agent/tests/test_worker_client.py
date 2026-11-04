import asyncio
from collections.abc import Callable

import httpx
import pytest

from agent.worker_client import (
    METRIC_SOURCES, RELOAD_PHASE1_PATH, RELOAD_PHASE2_PATH, VERSION_PATH, WorkerClient,
)

type Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> WorkerClient:
    return WorkerClient("http://worker", timeout=2.0, reload_timeout=30.0,
                        transport=httpx.MockTransport(handler))


def full_payloads() -> dict[str, dict[str, object]]:
    return {path: {name: 1.0 for name in names} for path, names in METRIC_SOURCES.items()}


def serve(payloads: dict[str, dict[str, object]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads.get(request.url.path)
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(404)
    return handler


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("worker is down")


# --- metrics ---

def test_metrics_reads_every_endpoint():
    assert asyncio.run(client(serve(full_payloads())).metrics()) is not None


def test_metrics_ignores_undeclared_names():
    payloads = full_payloads()
    next(iter(payloads.values()))["something.else"] = 1.0
    assert asyncio.run(client(serve(payloads)).metrics()) is not None


def test_metrics_missing_name_is_none():
    payloads = full_payloads()
    first = next(iter(payloads.values()))
    del first[next(iter(first))]
    assert asyncio.run(client(serve(payloads)).metrics()) is None


def test_missing_names_are_logged_once_until_they_come_back(caplog: pytest.LogCaptureFixture):
    payloads = full_payloads()
    first = next(iter(payloads.values()))
    removed = next(iter(first))
    value = first.pop(removed)
    c = client(serve(payloads))
    with caplog.at_level("INFO", logger="agent.worker"):
        asyncio.run(c.metrics())
        asyncio.run(c.metrics())
        assert [r.levelname for r in caplog.records] == ["WARNING"]       # once, not per reading
        first[removed] = value
        asyncio.run(c.metrics())
        del first[removed]
        asyncio.run(c.metrics())
    assert [r.levelname for r in caplog.records] == ["WARNING", "INFO", "WARNING"]


def test_metrics_one_failing_endpoint_is_none():
    payloads = full_payloads()
    del payloads[next(iter(payloads))]                  # that path now answers 404
    assert asyncio.run(client(serve(payloads)).metrics()) is None


def test_metrics_network_failure_is_none():
    assert asyncio.run(client(refuse).metrics()) is None


def test_metrics_invalid_value_is_none():
    payloads = full_payloads()
    first = next(iter(payloads.values()))
    first[next(iter(first))] = "not a number"
    assert asyncio.run(client(serve(payloads)).metrics()) is None


# --- version and health (the same endpoint) ---

def test_version():
    c = client(lambda r: httpx.Response(200, json={"version": "1.2.3"}))
    assert asyncio.run(c.version()) == "1.2.3"


def test_version_failure_is_none():
    assert asyncio.run(client(refuse).version()) is None


def test_health_uses_the_version_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"version": "1.2.3"})

    asyncio.run(client(handler).health())
    assert seen == [VERSION_PATH]


def test_health_distinguishes_unhealthy_from_unanswered():
    assert asyncio.run(client(lambda r: httpx.Response(200)).health()) is True
    assert asyncio.run(client(lambda r: httpx.Response(503)).health()) is False
    assert asyncio.run(client(refuse).health()) is None


# --- reload phases ---

def test_reload_phases_are_gets_to_their_paths():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200)

    c = client(handler)
    asyncio.run(c.reload_phase1())
    asyncio.run(c.reload_phase2())
    assert seen == [("GET", RELOAD_PHASE1_PATH), ("GET", RELOAD_PHASE2_PATH)]


def test_reload_raises_with_the_workers_explanation():
    c = client(lambda r: httpx.Response(500, text="bad artifact"))
    with pytest.raises(RuntimeError, match="HTTP 500: bad artifact"):
        asyncio.run(c.reload_phase1())


def test_reload_raises_on_network_failure():
    with pytest.raises(httpx.ConnectError):
        asyncio.run(client(refuse).reload_phase2())
