from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.feed import ConditionFeed, Conditions
from agent.inventory import FeedField, InventoryExporter, SettingField
from agent.wire import ProcessStatus

# The single feed values as fields (the repo sends desired and observed; config.yaml decides).
DEFAULT_FIELDS = (FeedField("process_status", "process_status"), FeedField("updating", "updating"),
                  FeedField("running_version", "running_version"))
RUNNING = Conditions(process_status=ProcessStatus.RUNNING, busy=False, updating=True, running_version="1.2.3")


def exporter(fields=DEFAULT_FIELDS, settings=None):
    return InventoryExporter(None, ConditionFeed(), settings=settings or SimpleNamespace(),  # type: ignore[arg-type]
                             interval=60, require_running=False, fields=fields)


def test_default_fields_are_the_feed_values():
    doc = exporter().record(RUNNING)
    assert doc.pop("at")
    assert doc == {"process_status": "running", "updating": True, "running_version": "1.2.3"}


def test_setting_fields_send_the_agent_setting():
    settings = SimpleNamespace(location="sg1", rack=7, etc_dir=Path("/srv/worker/etc"))
    fields = (FeedField("state", "process_status"), FeedField("busy", "busy"),
              SettingField("location", "location"), SettingField("rack", "rack"),
              SettingField("etc", "etc_dir"))
    doc = exporter(fields, settings).record(RUNNING)
    assert {k: v for k, v in doc.items() if k != "at"} == {
        "state": "running", "busy": False, "location": "sg1", "rack": 7, "etc": "/srv/worker/etc"}


def test_not_attached_sends_none():
    cond = Conditions(process_status=None, busy=False, updating=False, running_version=None)
    assert exporter().record(cond)["process_status"] is None


@pytest.mark.parametrize("fields, message", [
    ((FeedField("hostname", "busy"),), "clash"),
    ((FeedField("at", "busy"),), "clash"),
    ((FeedField("x", "busy"), FeedField("x", "updating")), "repeat"),
    ((FeedField("x", "pid"),), "unknown feed source"),
    ((SettingField("x", "missing"),), "no agent setting"),
])
def test_bad_fields_fail_at_construction(fields, message):
    with pytest.raises(ValueError, match=message):
        exporter(fields)



def _states():
    from ipaddress import IPv4Address

    from agent.wire import AgentStatus, DesiredState, HeartbeatReport
    desired = DesiredState(generation=4, restart_generation=1, force_update_generation=0, desired_running=True,
                           version="1.2.3", artifact_url="http://fs/a", artifact_sha256="ab",
                           hypertree_get_url="http://fs/h", hypertree_upload_url="http://fs/h")
    observed = HeartbeatReport(ipv4=IPv4Address("10.0.0.1"), acknowledged_generation=4, handled_restart_generation=1,
                               handled_force_update_generation=0, failed_generation=None, running_version="1.2.3",
                               process_status=ProcessStatus.RUNNING, healthy=True, violations=["slow"],
                               metrics_available=True, agent_status=AgentStatus.IDLE,
                               current_operation=None, last_operation=None)
    return desired, observed


STATE_FIELDS = (FeedField("desired", "desired"), FeedField("observed", "observed"))


def test_desired_and_observed_are_sent_flat_with_their_prefix():
    desired, observed = _states()
    cond = Conditions(process_status=ProcessStatus.RUNNING, busy=False, updating=False, running_version="1.2.3",
                      desired=desired, observed=observed)
    doc = exporter(STATE_FIELDS).record(cond)
    assert doc["desired_generation"] == 4 and doc["desired_version"] == "1.2.3"
    assert doc["desired_running"] is True                          # not desired_desired_running
    assert doc["observed_healthy"] is True and doc["observed_agent_status"] == "idle"
    assert doc["observed_process_status"] == "running" and doc["observed_running_version"] == "1.2.3"
    assert doc["observed_updating"] is False and doc["observed_busy"] is False   # from the feed
    assert "observed_ipv4" not in doc                              # every record has ipv4
    assert doc["observed_violations"] == ["slow"]
    assert not any(isinstance(v, dict) for v in doc.values())      # flat
    assert not {"observed_current_operation", "observed_last_operation", "desired", "observed"} & doc.keys()


def test_desired_and_observed_fields_are_none_before_the_first_heartbeat():
    desired, observed = _states()
    cond = Conditions(process_status=None, busy=False, updating=False, running_version=None)
    doc = exporter(STATE_FIELDS).record(cond)
    full = exporter(STATE_FIELDS).record(Conditions(process_status=None, busy=False, updating=False,
                                                    running_version=None, desired=desired, observed=observed))
    assert doc.keys() == full.keys()                               # the same fields, so the mapping is stable
    assert all(v is None for k, v in doc.items() if k not in ("at", "observed_updating", "observed_busy"))
    assert doc["observed_updating"] is False and doc["observed_busy"] is False   # always known


def test_a_flat_name_that_clashes_fails_at_construction():
    with pytest.raises(ValueError, match="repeat"):
        exporter((FeedField("desired_version", "running_version"), FeedField("desired", "desired")))
