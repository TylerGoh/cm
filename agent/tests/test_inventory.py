from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.feed import ConditionFeed, Conditions
from agent.inventory import FeedField, InventoryExporter, SettingField
from agent.wire import ProcessStatus

# The repo's three fields; config.yaml may change INVENTORY_FIELDS itself.
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
