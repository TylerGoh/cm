import asyncio
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.feed import ConditionFeed, Conditions
from agent.inventory import FeedField, InventoryExporter, SettingField
from agent.wire import AgentStatus, DesiredState, HeartbeatReport, ProcessStatus

# The single feed values as fields (the repo sends desired and observed; config.yaml decides).
FEED_FIELDS = (FeedField("process_status", "process_status"), FeedField("updating", "updating"),
               FeedField("running_version", "running_version"))
STATE_FIELDS = (FeedField("desired", "desired"), FeedField("observed", "observed"))
RUNNING = Conditions(process_status=ProcessStatus.RUNNING, busy=False, updating=True, running_version="1.2.3")


class FakeElastic:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def put_host(self, key: str, doc: dict[str, object]) -> None:
        self.calls.append(("put", (key, doc)))

    async def delete_other_host_docs(self, keep: list[str]) -> None:
        self.calls.append(("delete_others", keep))


def exporter(fields=FEED_FIELDS, settings=None, intfs=("eth0",), elastic=None, feed=None):
    return InventoryExporter(elastic, feed or ConditionFeed(), settings=settings or SimpleNamespace(),  # type: ignore[arg-type]
                             intfs=list(intfs), interval=60, require_running=False, fields=fields)


def without_at(doc: dict[str, object]) -> dict[str, object]:
    assert doc["at"]
    return {k: v for k, v in doc.items() if k != "at"}


def states() -> tuple[DesiredState, HeartbeatReport]:
    desired = DesiredState(generation=4, restart_generation=1, force_update_generation=0, desired_running=True,
                           version="1.2.3", artifact_url="http://fs/a", artifact_sha256="ab",
                           hypertree_get_url="http://fs/h", hypertree_upload_url="http://fs/h")
    observed = HeartbeatReport(ipv4=IPv4Address("10.0.0.1"), acknowledged_generation=4, handled_restart_generation=1,
                               handled_force_update_generation=0, failed_generation=None, running_version="1.2.3",
                               process_status=ProcessStatus.RUNNING, healthy=True, violations=["slow"],
                               metrics_available=True, agent_status=AgentStatus.IDLE,
                               current_operation=None, last_operation=None)
    return desired, observed


# --- inventory records: one per interface ---

def test_one_record_per_interface():
    docs = exporter(intfs=("eth0", "eth1")).documents(RUNNING)
    assert list(docs) == ["intf_eth0", "intf_eth1"]
    assert without_at(docs["intf_eth1"]) == {"kind": "inventory", "intf": "eth1", "process_status": "running",
                                             "updating": True, "running_version": "1.2.3"}


def test_setting_fields_send_the_agent_setting():
    settings = SimpleNamespace(location="sg1", rack=7, etc_dir=Path("/srv/worker/etc"))
    fields = (FeedField("status", "process_status"), FeedField("busy", "busy"),
              SettingField("location", "location"), SettingField("rack", "rack"),
              SettingField("etc", "etc_dir"))
    doc = exporter(fields, settings).documents(RUNNING)["intf_eth0"]
    assert without_at(doc) == {"kind": "inventory", "intf": "eth0", "status": "running", "busy": False,
                               "location": "sg1", "rack": 7, "etc": "/srv/worker/etc"}


def test_not_attached_sends_none():
    cond = Conditions(process_status=None, busy=False, updating=False, running_version=None)
    assert exporter().documents(cond)["intf_eth0"]["process_status"] is None


@pytest.mark.parametrize("intfs", [(), ("eth0", "eth0")])
def test_intf_must_name_each_interface_once(intfs):
    with pytest.raises(ValueError, match="intf"):
        exporter(intfs=intfs)


@pytest.mark.parametrize("fields, message", [
    ((FeedField("hostname", "busy"),), "clash"),
    ((FeedField("ip", "busy"),), "clash"),
    ((FeedField("intf", "busy"),), "clash"),
    ((FeedField("cluster_name", "busy"),), "clash"),
    ((FeedField("at", "busy"),), "clash"),
    ((FeedField("x", "busy"), FeedField("x", "updating")), "repeat"),
    ((FeedField("x", "pid"),), "unknown feed source"),
    ((SettingField("x", "missing"),), "no agent setting"),
    ((FeedField("desired_version", "running_version"), FeedField("desired", "desired")), "repeat"),
])
def test_bad_fields_fail_at_construction(fields, message):
    with pytest.raises(ValueError, match=message):
        exporter(fields)


# --- the state document: desired and observed, one per host ---

def test_desired_and_observed_go_in_one_state_document_flat():
    desired, observed = states()
    cond = Conditions(process_status=ProcessStatus.RUNNING, busy=False, updating=False, running_version="1.2.3",
                      desired=desired, observed=observed)
    docs = exporter(FEED_FIELDS + STATE_FIELDS, intfs=("eth0", "eth1")).documents(cond)
    assert list(docs) == ["intf_eth0", "intf_eth1", "state"]
    state = docs["state"]
    assert state["kind"] == "state" and "intf" not in state
    assert state["desired_generation"] == 4 and state["desired_version"] == "1.2.3"
    assert state["desired_running"] is True                        # not desired_desired_running
    assert state["observed_healthy"] is True and state["observed_agent_status"] == "idle"
    assert state["observed_process_status"] == "running" and state["observed_violations"] == ["slow"]
    assert state["observed_updating"] is False and state["observed_busy"] is False   # from the feed
    assert not {"observed_ipv4", "observed_current_operation", "observed_last_operation"} & state.keys()
    assert not any(isinstance(v, dict) for v in state.values())    # flat
    assert not any(k.startswith(("desired_", "observed_")) for k in docs["intf_eth0"])   # not in the records


def test_state_fields_are_none_before_the_first_heartbeat():
    desired, observed = states()
    before = exporter(STATE_FIELDS).documents(Conditions(process_status=None, busy=False, updating=False,
                                                         running_version=None))["state"]
    after = exporter(STATE_FIELDS).documents(Conditions(process_status=None, busy=False, updating=False,
                                                        running_version=None, desired=desired,
                                                        observed=observed))["state"]
    assert before.keys() == after.keys()                           # the same fields, so the mapping is stable
    assert all(v is None for k, v in before.items() if k not in ("at", "kind", "observed_updating", "observed_busy"))


def test_no_state_document_without_desired_or_observed():
    assert "state" not in exporter(FEED_FIELDS).documents(RUNNING)


# --- a push: write under fixed ids, then delete the rest ---

def test_a_push_writes_every_document_then_deletes_the_others():
    elastic, feed = FakeElastic(), ConditionFeed()
    feed.publish(RUNNING)
    asyncio.run(exporter(FEED_FIELDS + STATE_FIELDS, intfs=("eth0", "eth1"), elastic=elastic, feed=feed)._step())
    assert [(c[0], c[1][0] if c[0] == "put" else c[1]) for c in elastic.calls] == [
        ("put", "intf_eth0"), ("put", "intf_eth1"), ("put", "state"),
        ("delete_others", ["intf_eth0", "intf_eth1", "state"]),       # last, so the host is never missing
    ]


def test_require_running_sends_and_deletes_nothing_while_not_running():
    elastic = FakeElastic()
    m = InventoryExporter(elastic, ConditionFeed(), settings=SimpleNamespace(), intfs=["eth0"],  # type: ignore[arg-type]
                          interval=60, require_running=True, fields=FEED_FIELDS)
    asyncio.run(m._step())
    assert elastic.calls == []
