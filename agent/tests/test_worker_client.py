import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from agent.worker_client import (
    METRIC_SOURCES, RELOAD_PHASE1_COMMAND, RELOAD_PHASE2_COMMAND, VERSION_COMMAND, WorkerClient,
)

type Handler = Callable[[httpx.Request], httpx.Response]

BASE_URL = "http://worker/api?command="     # the real shape: commands are appended to a query

# What the version command answers: a header row, then one row per installed version.
VERSIONS = [["type", "version", "active"], ["app", "1.2.2", "N"], ["app", "1.2.3", "Y"]]


class AnySettings:
    """Stands in for the agent's config: commands are format strings over its settings
    (config.yaml decides which), so every setting reads as /<name>."""
    def __getattr__(self, name: str) -> str:
        return f"/{name}"


SETTINGS = AnySettings()


def filled(command: str) -> str:
    return command.format(config=SETTINGS)


def client(handler: Handler) -> WorkerClient:
    return WorkerClient(BASE_URL, timeout=2.0, reload_timeout=30.0, settings=SETTINGS,
                        transport=httpx.MockTransport(handler))


def command_of(request: httpx.Request) -> str:
    """The command a request was for: everything after the base URL."""
    url = str(request.url)
    assert url.startswith(BASE_URL), url
    return unquote(url.removeprefix(BASE_URL))


def full_payloads() -> dict[str, dict[str, object]]:
    return {filled(command): {name: 1.0 for name in names} for command, names in METRIC_SOURCES.items()}


def serve(payloads: dict[str, dict[str, object]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads.get(command_of(request))
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(404)
    return handler


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("worker is down")


# --- URLs ---

def test_command_is_appended_to_the_base_url_as_is():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=VERSIONS)

    asyncio.run(client(handler).version())
    assert seen == [BASE_URL + filled(VERSION_COMMAND)]


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
    c = client(lambda r: httpx.Response(200, json=VERSIONS))
    assert asyncio.run(c.version()) == "1.2.3"


def test_version_finds_columns_by_header():
    table = [["active", "type", "version"], ["N", "app", "0.1"], ["Y", "other", "9"], ["Y", "app", "0.2"]]
    assert asyncio.run(client(lambda r: httpx.Response(200, json=table)).version()) == "0.2"


def test_version_ignores_case():
    table = [["type", "version", "active"], ["App", "0.1", "n"], ["APP", "0.2", "y"]]
    assert asyncio.run(client(lambda r: httpx.Response(200, json=table)).version()) == "0.2"


def test_version_no_active_app_is_none():
    table = [["type", "version", "active"], ["app", "0.1", "N"], ["lib", "0.2", "Y"]]
    assert asyncio.run(client(lambda r: httpx.Response(200, json=table)).version()) is None


def test_version_unexpected_shape_is_none():
    for payload in ({"version": "1.2.3"}, [], [["type", "version"]], [["type", "version", "active"], ["app"]]):
        assert asyncio.run(client(lambda r, p=payload: httpx.Response(200, json=p)).version()) is None


def test_version_failure_is_none():
    assert asyncio.run(client(refuse).version()) is None


def test_health_uses_the_version_command():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(command_of(request))
        return httpx.Response(200, json=VERSIONS)

    asyncio.run(client(handler).health())
    assert seen == [VERSION_COMMAND]


def test_health_distinguishes_unhealthy_from_unanswered():
    assert asyncio.run(client(lambda r: httpx.Response(200)).health()) is True
    assert asyncio.run(client(lambda r: httpx.Response(503)).health()) is False
    assert asyncio.run(client(refuse).health()) is None


# --- reload phases ---

def test_reload_phases_are_gets_to_their_commands():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, command_of(request)))
        return httpx.Response(200)

    c = client(handler)
    asyncio.run(c.reload_phase1())
    asyncio.run(c.reload_phase2())
    assert seen == [("GET", filled(RELOAD_PHASE1_COMMAND)), ("GET", filled(RELOAD_PHASE2_COMMAND))]


def test_reload_raises_with_the_workers_explanation():
    c = client(lambda r: httpx.Response(500, text="bad artifact"))
    with pytest.raises(RuntimeError, match="HTTP 500: bad artifact"):
        asyncio.run(c.reload_phase1())


def test_reload_raises_on_network_failure():
    with pytest.raises(httpx.ConnectError):
        asyncio.run(client(refuse).reload_phase2())


def test_commands_are_filled_from_the_settings():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    c = WorkerClient(BASE_URL, timeout=2.0, reload_timeout=30.0,
                     settings=SimpleNamespace(etc_dir=Path("/srv/worker/etc")),
                     transport=httpx.MockTransport(handler))
    asyncio.run(c._command("reload_app_phase2{config.etc_dir}"))
    asyncio.run(c._command("literal{{braces}}"))
    assert seen == [BASE_URL + "reload_app_phase2/srv/worker/etc", BASE_URL + "literal{braces}"]
