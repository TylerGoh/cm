from agent.models import AgentMemory, Observed
from agent.wire import DesiredState, OpType, ProcessStatus


def decide(desired: DesiredState, observed: Observed, memory: AgentMemory) -> OpType | None:
    status = observed.process_status
    if status is None:                                   # not attached yet
        return OpType.REATTACH
    if status in (ProcessStatus.STARTING, ProcessStatus.STOPPING):
        return None
    if status is ProcessStatus.CRASHED:
        if desired.force_update_generation > memory.handled_force_update_generation:
            return OpType.FORCE_UPDATE
        if desired.desired_running and not memory.crash_restarted:
            return OpType.CRASH_RESTART
        return None
    if desired.generation == memory.failed_generation:
        return None
    if not desired.desired_running:
        return OpType.STOP if status is ProcessStatus.RUNNING else None
    if status is ProcessStatus.STOPPED:
        return OpType.START
    if observed.running_version is None:
        return None
    if observed.running_version != desired.version:
        return OpType.UPDATE
    if desired.restart_generation > memory.handled_restart_generation:
        return OpType.RESTART
    return None
