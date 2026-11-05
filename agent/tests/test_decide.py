"""decide(): the force bump from CRASHED and STOPPED, and the failed-generation guard."""

from agent.decide import decide
from agent.models import AgentMemory, Observed
from agent.wire import DesiredState, OpType, ProcessStatus


def desired(*, generation: int = 5, force: int = 0, running: bool = True) -> DesiredState:
    return DesiredState(generation=generation, restart_generation=0, force_update_generation=force,
                        desired_running=running, version="job_2", artifact_url="http://fs/a",
                        artifact_sha256="0" * 64, hypertree_get_url="http://fs/h", hypertree_upload_url="http://fs/h")


def memory(*, failed: int | None = None, force_handled: int = 0, crash_restarted: bool = False) -> AgentMemory:
    return AgentMemory(acknowledged_generation=5, handled_restart_generation=0,
                       handled_force_update_generation=force_handled, failed_generation=failed,
                       crash_restarted=crash_restarted)


def observed(status: ProcessStatus) -> Observed:
    return Observed(process_status=status, healthy=None, running_version=None)


def test_a_force_bump_runs_from_crashed() -> None:
    assert decide(desired(force=1), observed(ProcessStatus.CRASHED), memory(crash_restarted=True)) is OpType.FORCE_UPDATE


def test_a_force_bump_runs_from_stopped_even_after_a_failed_start() -> None:
    # an agent restart turned a crash-held worker into STOPPED, with the generation already failed
    assert decide(desired(), observed(ProcessStatus.STOPPED), memory(failed=5)) is None
    assert decide(desired(generation=6, force=1), observed(ProcessStatus.STOPPED), memory(failed=5)) is OpType.FORCE_UPDATE


def test_after_the_force_update_a_stopped_worker_starts() -> None:
    assert decide(desired(generation=6, force=1), observed(ProcessStatus.STOPPED),
                  memory(failed=5, force_handled=1)) is OpType.START


def test_a_force_bump_on_a_worker_kept_down_only_downloads() -> None:
    assert decide(desired(generation=6, force=1, running=False), observed(ProcessStatus.STOPPED),
                  memory()) is OpType.FORCE_UPDATE
    assert decide(desired(generation=6, force=1, running=False), observed(ProcessStatus.STOPPED),
                  memory(force_handled=1)) is None


def test_a_force_bump_waits_while_running() -> None:
    assert decide(desired(force=1), observed(ProcessStatus.RUNNING), memory()) is None
