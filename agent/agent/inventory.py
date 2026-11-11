import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from agent.elastic import ElasticClient
from agent.feed import ConditionFeed, Conditions
from agent.wire import DesiredState, HeartbeatReport, ProcessStatus

log = logging.getLogger("agent.inventory")

# Keys every host index document carries besides INVENTORY_FIELDS: `at`, `kind` (inventory or
# state) and, on an inventory record, `intf` from here; hostname, ip and cluster_name from
# ElasticClient. prepare.py refuses them as fields, and so does the constructor.
RECORD_KEYS = frozenset({"at", "kind", "intf", "hostname", "ip", "cluster_name"})
STATE_KEY = "state"                                 # the state document's id is <ip>_state
FEED_SOURCES = frozenset({"process_status", "updating", "running_version", "busy", "desired", "observed"})

# Feed sources that are whole states. Each is sent flat: one top-level field per value, named
# <field>_<value name> (desired_version, observed_healthy), or the value's own name when it already
# starts with <field>_ (desired_running). Only plain values and lists: the operations are left
# out, and so is the report's ipv4, which every document has as ip. observed also carries the
# feed's updating and busy, so it is the agent's whole observed state in one group. These go in
# the host's one state document; every other field goes in each inventory record.
OBJECT_SOURCES: dict[str, type] = {"desired": DesiredState, "observed": HeartbeatReport}
LEFT_OUT = frozenset({"current_operation", "last_operation", "ipv4"})
OBSERVED_FROM_FEED = ("updating", "busy")


def _state_names(source: str) -> list[str]:
    names = [n for n in OBJECT_SOURCES[source].model_fields if n not in LEFT_OUT]
    return names + list(OBSERVED_FROM_FEED) if source == "observed" else names


def _flat_names(field: str, source: str) -> dict[str, str]:
    """For an object source: record key -> the value's name."""
    return {(name if name.startswith(f"{field}_") else f"{field}_{name}"): name
            for name in _state_names(source)}


def _record_keys(f: "FeedField | SettingField") -> list[str]:
    if isinstance(f, FeedField) and f.source in OBJECT_SOURCES:
        return list(_flat_names(f.field, f.source))
    return [f.field]


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
    FeedField("desired", "desired"),    # desired_*: the last desired state from the cluster manager
    FeedField("observed", "observed"),    # observed_*: the agent's state as its heartbeat reports it
)
# --- END generated: inventory fields ---


def _check(fields: tuple[FeedField | SettingField, ...], settings: object) -> None:
    for f in fields:
        if isinstance(f, FeedField) and f.source not in FEED_SOURCES:
            raise ValueError(f"inventory field {f.field}: unknown feed source {f.source!r}")
    names = [key for f in fields for key in _record_keys(f)]
    clashing = RECORD_KEYS & set(names)
    if clashing:
        raise ValueError(f"inventory fields clash with record keys: {sorted(clashing)}")
    if len(names) != len(set(names)):
        raise ValueError(f"inventory fields repeat a name: {names}")
    for f in fields:
        if isinstance(f, SettingField) and not hasattr(settings, f.setting):
            raise ValueError(f"inventory field {f.field}: no agent setting {f.setting!r}")


def _plain(value: object) -> object:
    """JSON-ready: enums by value, lists item by item, paths and addresses as text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return str(value)


class InventoryExporter:
    """Every interval, keeps this host's documents in Elastic's host index current:
    - one inventory record per interface in intfs, id <ip>_intf_<intf>: `at`, `kind: inventory`,
      `intf`, then each INVENTORY_FIELDS field that isn't a whole state;
    - one state document, id <ip>_state: `at`, `kind: state`, then the desired_* and observed_*
      fields (only when INVENTORY_FIELDS has desired or observed).
    A write under a fixed id replaces the last one; then every other document of this host is
    deleted (an interface no longer in intfs, records from before the fixed ids), except events.
    The host's identity is added by ElasticClient. With require_running, it does nothing unless
    the process is RUNNING."""

    def __init__(self, elastic: ElasticClient, feed: ConditionFeed, *, settings: object,
                 intfs: list[str], interval: float, require_running: bool,
                 fields: tuple[FeedField | SettingField, ...] = INVENTORY_FIELDS) -> None:
        _check(fields, settings)                    # a bad field fails startup
        if not intfs or len(set(intfs)) != len(intfs):
            raise ValueError(f"inventory: intf must name at least one interface, each once: {intfs}")
        self._elastic = elastic
        self._feed = feed
        self._settings = settings
        self._intfs = list(intfs)
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

    def documents(self, cond: Conditions) -> dict[str, dict[str, object]]:
        """Every document this host keeps in the host index, by key (the id after <ip>_)."""
        at = datetime.now(UTC).isoformat()
        record: dict[str, object] = {}
        state: dict[str, object] = {}
        for f in self._fields:
            if isinstance(f, FeedField) and f.source in OBJECT_SOURCES:
                value = getattr(cond, f.source)              # None before the first heartbeat
                for key, name in _flat_names(f.field, f.source).items():
                    if f.source == "observed" and name in OBSERVED_FROM_FEED:
                        state[key] = _plain(getattr(cond, name))   # always known
                    else:
                        state[key] = _plain(getattr(value, name)) if value is not None else None
            elif isinstance(f, FeedField):
                record[f.field] = _plain(getattr(cond, f.source))
            else:
                record[f.field] = _plain(getattr(self._settings, f.setting))
        docs = {f"intf_{intf}": {"at": at, "kind": "inventory", "intf": intf, **record}
                for intf in self._intfs}
        if state:
            docs[STATE_KEY] = {"at": at, "kind": "state", **state}
        return docs

    async def _step(self) -> None:
        cond = self._feed.current()
        if self._require_running and cond.process_status is not ProcessStatus.RUNNING:
            return
        docs = self.documents(cond)
        for key, doc in docs.items():
            await self._elastic.put_host(key, doc)
        await self._elastic.delete_other_host_docs(keep=list(docs))
