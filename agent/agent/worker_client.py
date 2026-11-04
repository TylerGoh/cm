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
    path: str


class MetricType(StrEnum):
    COUNTER = auto()    # cumulative: the amount over a window is newest - oldest
    GAUGE = auto()      # snapshot: the value is the current reading


# --- the worker's contract (MOCK paths and names; replace with the real ones) ---
COUNTERS_ENDPOINT = MetricSource("/counters")
VERSION_PATH = "/show_version"          # the cheapest ping: version() and health() both use it
RELOAD_PHASE1_PATH = "/reload_phase1"   # GET; the two phases are always called back to back
RELOAD_PHASE2_PATH = "/reload_phase2"   # GET


class WorkerMetrics(BaseModel):
    """One flat reading of the worker at one instant. Every field declares a MetricType,
    a MetricSource and the dotted name it has at that endpoint (validation_alias)."""
    model_config = ConfigDict(frozen=True)

    # /counters, all cumulative since the worker started
    requests_out_total:        Annotated[float, MetricType.COUNTER, COUNTERS_ENDPOINT, Field(validation_alias="requests.out.total")]
    requests_old_failed_total: Annotated[float, MetricType.COUNTER, COUNTERS_ENDPOINT, Field(validation_alias="requests.old_failed.total")]
    requests_out_lagged_total: Annotated[float, MetricType.COUNTER, COUNTERS_ENDPOINT, Field(validation_alias="requests.out_lagged.total")]


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
        sources.setdefault(source.path, set()).add(alias)
        (counters if metric_type is MetricType.COUNTER else gauges).add(name)
    return sources, frozenset(counters), frozenset(gauges)


# Derived at import, so a badly declared field fails at startup.
METRIC_SOURCES, COUNTER_FIELDS, GAUGE_FIELDS = _derive()


class WorkerClient:
    """The only agent code that talks to the worker."""

    def __init__(self, base_url: str, *, timeout: float, reload_timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._reload_timeout = reload_timeout       # the reload phases are the only slow calls

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_json(self, path: str) -> dict[str, object]:
        response = await self._http.get(path)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return payload

    # --- queries: never raise, return None on failure ---

    async def metrics(self) -> WorkerMetrics | None:
        """All or nothing: one reading describes one instant. Returns None on any failure."""
        paths = list(METRIC_SOURCES)
        results = await asyncio.gather(*(self._get_json(p) for p in paths), return_exceptions=True)

        values: dict[str, object] = {}
        for path, payload in zip(paths, results, strict=True):
            if isinstance(payload, BaseException):
                if not isinstance(payload, Exception):
                    raise payload                    # a cancellation, not a failed fetch
                return None                          # network or HTTP failure: silent
            missing = METRIC_SOURCES[path] - payload.keys()
            if missing:
                log.warning("%s is missing %s", path, sorted(missing))
                return None
            values.update({name: payload[name] for name in METRIC_SOURCES[path]})   # declared names only

        try:
            return WorkerMetrics.model_validate(values)
        except ValidationError:
            log.exception("worker metrics failed validation")
            return None

    async def version(self) -> str | None:
        try:
            payload = await self._get_json(VERSION_PATH)
            return str(payload["version"])
        except Exception:
            return None

    async def health(self) -> bool | None:
        """The version endpoint as a liveness ping. True = it answered, False = it answered
        with an error, None = no answer."""
        try:
            response = await self._http.get(VERSION_PATH)
        except Exception:
            return None
        return response.is_success

    # --- commands: raise, because pipeline steps use them ---

    async def reload_phase1(self) -> None:
        await self._command(RELOAD_PHASE1_PATH)

    async def reload_phase2(self) -> None:
        await self._command(RELOAD_PHASE2_PATH)

    async def _command(self, path: str) -> None:
        """Raises on failure, with the worker's own explanation, since the step record keeps only
        the error string. Network failures propagate as httpx errors."""
        response = await self._http.get(path, timeout=self._reload_timeout)
        if response.is_error:
            raise RuntimeError(f"{path}: HTTP {response.status_code}: {response.text[:200]}")
