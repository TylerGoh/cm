import asyncio
import logging
import time
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel

from config import config
from agent.elastic import ElasticClient
from agent.feed import ConditionFeed, Conditions
from agent.wire import DesiredState, HeartbeatReport, ProcessStatus

log = logging.getLogger("agent.inventory")

# BEGIN generated: inventory fields
INVENTORY_FIELDS: dict[str, object] = {
}
# END generated: inventory fields

# Not sent from the desired and observed states: the operations are nested objects, and ipv4 is
# already every document's ip.
LEFT_OUT = frozenset({"current_operation", "last_operation", "ipv4"})


def _plain(value: object) -> object:
    """JSON-ready: enums by value, lists item by item, paths and addresses as text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _flat(prefix: str, model: type[BaseModel], state: BaseModel | None) -> dict[str, object]:
    """Each field of the state as its own field: version -> desired_version; one already named
    <prefix>_... keeps its name (desired_running). Every value is None before the first heartbeat."""
    fields = {}
    for name in model.model_fields:
        if name in LEFT_OUT:
            continue
        key = name if name.startswith(f"{prefix}_") else f"{prefix}_{name}"
        fields[key] = _plain(getattr(state, name)) if state is not None else None
    return fields


class InventoryExporter:
    """Every interval, writes this host's documents to the host index, each under a fixed id so it
    replaces the last one, then deletes the host's other documents:
    - one inventory record per interface, <ip>_intf_<intf>, with INVENTORY_FIELDS;
    - one state document, <ip>_state, with the desired and observed states.
    With require_running, it does nothing unless the process is RUNNING."""

    def __init__(self, elastic: ElasticClient, feed: ConditionFeed, *, intfs: list[str],
                 interval: float, require_running: bool) -> None:
        if not intfs or len(set(intfs)) != len(intfs):
            raise ValueError(f"inventory: intf must name at least one interface, each once: {intfs}")
        self._elastic = elastic
        self._feed = feed
        self._intfs = list(intfs)
        self._interval = interval
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
        fields = {name: _plain(value) for name, value in INVENTORY_FIELDS.items()}
        docs: dict[str, dict[str, object]] = {
            f"intf_{intf}": {"at": at, "kind": "inventory", "intf": intf, **fields}
            for intf in self._intfs
        }
        docs["state"] = {
            "at": at,
            "kind": "state",
            **_flat("desired", DesiredState, cond.desired),
            **_flat("observed", HeartbeatReport, cond.observed),
            "observed_updating": cond.updating,
            "observed_busy": cond.busy,
        }
        return docs

    async def _step(self) -> None:
        cond = self._feed.current()
        if self._require_running and cond.process_status is not ProcessStatus.RUNNING:
            return
        docs = self.documents(cond)
        for key, doc in docs.items():
            await self._elastic.put_host(key, doc)
        await self._elastic.delete_other_host_docs(keep=list(docs))
