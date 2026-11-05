from pathlib import Path

import pytest

from agent.models import ProcessMemory
from agent.operations import plan
from agent.process import ProcessManager
from agent.state_store import JsonStore
from agent.updates import UpdateManager
from agent.wire import DesiredState, OpType
from agent.worker_client import WorkerClient

DESIRED = DesiredState(
    generation=3, restart_generation=1, force_update_generation=0, desired_running=True,
    version="job_7", artifact_url="http://fs/jobs/job_7/artifact", artifact_sha256="0" * 64,
    hypertree_get_url="http://fs/jobs/job_7/hypertree", hypertree_upload_url="http://fs/jobs/job_7/hypertree",
)


def managers(tmp_path: Path) -> tuple[ProcessManager, UpdateManager]:
    worker = WorkerClient("http://worker", timeout=1, reload_timeout=1)
    process = ProcessManager(worker, JsonStore(ProcessMemory, tmp_path / "process.json"),
                             command=["true"], console_log_dir=tmp_path, program_log_path=tmp_path / "w.log",
                             initialize_flag="ready", start_timeout=1, stop_timeout=1)
    updates = UpdateManager(worker, download_dir=tmp_path, bundle_dir=tmp_path, etc_dir=tmp_path,
                            bundle_symlink_mapping={}, bundle_symlink_default_dir=None,
                            hypertree_dir=tmp_path,
                            download_timeout=1, hypertree_timeout=1, verify_timeout=1, verify_interval=1)
    return process, updates


@pytest.mark.parametrize("op", list(OpType))
def test_every_op_builds_and_its_context_is_complete(op: OpType, tmp_path: Path) -> None:
    process, updates = managers(tmp_path)
    recipe = plan(op, DESIRED, process, updates)
    recipe.pipeline._check_context(recipe.context)       # every initial key present, right type


def test_step_names_and_settings(tmp_path: Path) -> None:
    process, updates = managers(tmp_path)
    update = plan(OpType.UPDATE, DESIRED, process, updates).pipeline.steps
    assert [s.stage.name for s in update] == [
        "download", "clear_hypertree", "fetch_hypertree", "reload", "verify", "upload_hypertree",
    ]
    assert [s.critical for s in update] == [True, False, False, True, True, False]
    assert update[4].retries == 1

    force_update = plan(OpType.FORCE_UPDATE, DESIRED, process, updates).pipeline.steps
    assert [s.stage.name for s in force_update] == ["download", "clear_hypertree", "fetch_hypertree"]
    assert [s.critical for s in force_update] == [True, False, False]

    crash_restart = plan(OpType.CRASH_RESTART, DESIRED, process, updates).pipeline.steps
    assert "upload_hypertree" not in [s.stage.name for s in crash_restart]

    start = plan(OpType.START, DESIRED, process, updates).pipeline.steps
    assert [(s.stage.name, s.retries) for s in start] == [("start", 0), ("wait_for_log", 0)]

    reattach = plan(OpType.REATTACH, DESIRED, process, updates).pipeline.steps
    assert [s.stage.name for s in reattach] == ["reattach", "wait_for_version", "stop"]
    assert reattach[1].retries == 0 and reattach[1].when is not None and reattach[2].when is not None
