import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("agent.worker")


@dataclass(frozen=True, slots=True)
class MetricSource:
    command: str


class MetricType(StrEnum):
    COUNTER = auto()    # cumulative: the amount over a window is newest - oldest
    GAUGE = auto()      # snapshot: the value is the current reading


# --- BEGIN generated: worker contract (prepare.py, from config.yaml's worker: section) ---

# Commands on the worker, each appended as-is to the base URL (WORKER_URL). Every query and
# command is a GET with no body. Each is a format string: {config.<setting>} becomes that agent
# setting when the request is made.
VERSION_COMMAND = "show_version"           # version() and health() both use it
RELOAD_PHASE1_COMMAND = "reload_phase1"    # blocks until done; success means done
RELOAD_PHASE2_COMMAND = "reload_phase2"    # blocks until the worker runs the new code

# Metric commands: each answers one flat JSON object.
COUNTERS_ENDPOINT = MetricSource("counters")


class WorkerMetrics(BaseModel):
    """One flat reading of the worker at one instant. Every field declares a MetricType,
    a MetricSource and the name it has in that command's answer (validation_alias)."""
    model_config = ConfigDict(frozen=True)

    # counters
    requests_out_total:        Annotated[float, MetricType.COUNTER, COUNTERS_ENDPOINT, Field(validation_alias="requests.out.total")]    # sent out
    requests_old_failed_total: Annotated[float, MetricType.COUNTER, COUNTERS_ENDPOINT, Field(validation_alias="requests.old_failed.total")]    # failed because too old
    requests_out_lagged_total: Annotated[float, MetricType.COUNTER, COUNTERS_ENDPOINT, Field(validation_alias="requests.out_lagged.total")]    # failed because lagged
# --- END generated: worker contract ---


def _derive() -> tuple[dict[str, set[str]], frozenset[str], frozenset[str]]:
    sources: dict[str, set[str]] = {}       # endpoint → names at that endpoint
    counters: set[str] = set()              # WorkerMetrics field names
    gauges: set[str] = set()
    seen: set[str] = set()
    for name, info in WorkerMetrics.model_fields.items():
        source = next((m for m in info.metadata if isinstance(m, MetricSource)), None)
        metric_type = next((m for m in info.metadata if isinstance(m, MetricType)), None)
        alias = info.validation_alias
        if source is None or metric_type is None or not isinstance(alias, str):
            raise TypeError(f"WorkerMetrics.{name}: needs a MetricSource, a MetricType and a string validation_alias")
        if alias in seen:
            raise TypeError(f"WorkerMetrics.{name}: alias {alias!r} is used twice")
        seen.add(alias)
        sources.setdefault(source.command, set()).add(alias)
        (counters if metric_type is MetricType.COUNTER else gauges).add(name)
    return sources, frozenset(counters), frozenset(gauges)


# Derived at import, so a badly declared field fails at startup.
METRIC_SOURCES, COUNTER_FIELDS, GAUGE_FIELDS = _derive()


class WorkerClient:
    """The only agent code that talks to the worker. base_url is used as a plain prefix: each
    request goes to base_url + command, with nothing added between them."""

    def __init__(self, base_url: str, *, timeout: float, reload_timeout: float,
                 settings: object = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        # No base_url on the httpx client: httpx would add a "/" to a base URL that ends in a query.
        self._base_url = base_url
        self._settings = settings                   # the agent's config: fills {config.<setting>} in commands
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._reload_timeout = reload_timeout       # the reload phases are the only slow calls
        self._names_missing = False                 # warned once; cleared when every name is back

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, command: str) -> str:
        """A command is a format string (prepare.py checks each {config.<setting>} it names);
        one without fields comes back unchanged."""
        return self._base_url + command.format(config=self._settings)

    async def _get_json(self, command: str) -> dict[str, object]:
        response = await self._http.get(self._url(command))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{command}: expected a JSON object")
        return payload

    # --- queries: never raise, return None on failure ---

    async def metrics(self) -> WorkerMetrics | None:
        """All or nothing: one reading describes one instant. Returns None on any failure.
        Missing names are logged once, and again only after a reading had every name."""
        commands = list(METRIC_SOURCES)
        results = await asyncio.gather(*(self._get_json(c) for c in commands), return_exceptions=True)

        values: dict[str, object] = {}
        for command, payload in zip(commands, results, strict=True):
            if isinstance(payload, BaseException):
                if not isinstance(payload, Exception):
                    raise payload                    # a cancellation, not a failed fetch
                return None                          # network or HTTP failure: silent
            missing = METRIC_SOURCES[command] - payload.keys()
            if missing:
                if not self._names_missing:
                    log.warning("%s is missing %s; not logged again until every name is back",
                                command, sorted(missing))
                    self._names_missing = True
                return None
            values.update({name: payload[name] for name in METRIC_SOURCES[command]})   # declared names only

        if self._names_missing:
            log.info("worker metrics have every name again")
            self._names_missing = False
        try:
            return WorkerMetrics.model_validate(values)
        except ValidationError:
            log.exception("worker metrics failed validation")
            return None

    async def version(self) -> str | None:
        try:
            payload = await self._get_json(VERSION_COMMAND)
            return str(payload["version"])
        except Exception:
            return None

    async def health(self) -> bool | None:
        """The version command as a liveness ping. True = it answered, False = it answered
        with an error, None = no answer."""
        try:
            response = await self._http.get(self._url(VERSION_COMMAND))
        except Exception:
            return None
        return response.is_success

    # --- commands: raise, because pipeline steps use them ---

    async def reload_phase1(self) -> None:
        await self._command(RELOAD_PHASE1_COMMAND)

    async def reload_phase2(self) -> None:
        await self._command(RELOAD_PHASE2_COMMAND)

    async def _command(self, command: str) -> None:
        """Raises on failure, with the worker's own explanation, since the step record keeps only
        the error string. Network failures propagate as httpx errors."""
        response = await self._http.get(self._url(command), timeout=self._reload_timeout)
        if response.is_error:
            raise RuntimeError(f"{command}: HTTP {response.status_code}: {response.text[:200]}")
