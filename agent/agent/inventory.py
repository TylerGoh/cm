import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from agent.elastic import ElasticClient
from agent.feed import ConditionFeed, Conditions
from agent.wire import ProcessStatus

log = logging.getLogger("agent.inventory")

# Keys every host index record carries besides INVENTORY_FIELDS: `at` from here, hostname and
# ipv4 from ElasticClient. prepare.py refuses them as fields, and so does the constructor.
RECORD_KEYS = frozenset({"at", "hostname", "ipv4"})
FEED_SOURCES = frozenset({"process_status", "updating", "running_version", "busy"})


@dataclass(frozen=True, slots=True)
class FeedField:
    """A record field whose value is read from the condition feed on every send."""
    field: str
    source: str             # one of FEED_SOURCES


@dataclass(frozen=True, slots=True)
class SettingField:
    """A record field whose value is an agent .env setting (declared in config.py)."""
    field: str
    setting: str            # the config attribute, lower case


# --- BEGIN generated: inventory fields (prepare.py, from config.yaml's inventory: section) ---
INVENTORY_FIELDS: tuple[FeedField | SettingField, ...] = (
    FeedField("process_status", "process_status"),    # None while not attached
    FeedField("updating", "updating"),
    FeedField("running_version", "running_version"),    # None unless RUNNING
)
# --- END generated: inventory fields ---


def _check(fields: tuple[FeedField | SettingField, ...], settings: object) -> None:
    names = [f.field for f in fields]
    clashing = RECORD_KEYS & set(names)
    if clashing:
        raise ValueError(f"inventory fields clash with record keys: {sorted(clashing)}")
    if len(names) != len(set(names)):
        raise ValueError(f"inventory fields repeat a name: {names}")
    for f in fields:
        if isinstance(f, FeedField) and f.source not in FEED_SOURCES:
            raise ValueError(f"inventory field {f.field}: unknown feed source {f.source!r}")
        if isinstance(f, SettingField) and not hasattr(settings, f.setting):
            raise ValueError(f"inventory field {f.field}: no agent setting {f.setting!r}")


def _plain(value: object) -> object:
    """JSON-ready: enums by value, paths and addresses as text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return value.value
    return str(value)


class InventoryExporter:
    """Every interval, sends Elastic's host index one record of this host: `at`, then each of
    INVENTORY_FIELDS, read from the feed or the agent's settings. The host's identity is added
    by ElasticClient. With require_running, it sends nothing unless the process is RUNNING."""

    def __init__(self, elastic: ElasticClient, feed: ConditionFeed, *, settings: object,
                 interval: float, require_running: bool,
                 fields: tuple[FeedField | SettingField, ...] = INVENTORY_FIELDS) -> None:
        _check(fields, settings)                    # a bad field fails startup
        self._elastic = elastic
        self._feed = feed
        self._settings = settings
        self._fields = fields
        self._interval = interval                   # at least one heartbeat tick; shorter only sends duplicates
        self._require_running = require_running

    async def run(self) -> None:
        next_at = time.monotonic()
        while True:
            try:
                await self._step()
            except Exception:
                log.exception("inventory step failed")
            next_at = max(next_at + self._interval, time.monotonic())     # steady cadence
            await asyncio.sleep(next_at - time.monotonic())

    def record(self, cond: Conditions) -> dict[str, object]:
        doc: dict[str, object] = {"at": datetime.now(UTC).isoformat()}
        for f in self._fields:
            if isinstance(f, FeedField):
                doc[f.field] = _plain(getattr(cond, f.source))
            else:
                doc[f.field] = _plain(getattr(self._settings, f.setting))
        return doc

    async def _step(self) -> None:
        cond = self._feed.current()
        if self._require_running and cond.process_status is not ProcessStatus.RUNNING:
            return
        await self._elastic.index_host(self.record(cond))
