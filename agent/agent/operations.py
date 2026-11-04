from collections.abc import Mapping
from dataclasses import dataclass

from agent.pipeline import Guard, Pipeline, PipelineStep, Stage
from agent.process import ProcessManager, ReattachPlan
from agent.updates import UpdateManager
from agent.wire import DesiredState, OpType


def _wait_for_start(reattach_plan: ReattachPlan) -> bool:
    return reattach_plan.wait_for_start


def _wait_for_stop(reattach_plan: ReattachPlan) -> bool:
    return reattach_plan.wait_for_stop


WAIT_FOR_START = Guard.from_callable(_wait_for_start)
WAIT_FOR_STOP = Guard.from_callable(_wait_for_stop)


class _Stages:
    def __init__(self, process: ProcessManager, updates: UpdateManager) -> None:
        self.reattach = Stage.from_callable(process.reattach, produces="reattach_plan")
        self.start = Stage.from_callable(process.start, produces="log_offset")
        self.wait_for_log = Stage.from_callable(process.wait_for_log)
        self.wait_for_version = Stage.from_callable(process.wait_for_version)
        self.stop = Stage.from_callable(process.stop)
        self.download = Stage.from_callable(updates.download)
        self.clear_hypertree = Stage.from_callable(updates.clear_hypertree)
        self.fetch_hypertree = Stage.from_callable(updates.fetch_hypertree)
        self.reload = Stage.from_callable(updates.reload)
        self.verify = Stage.from_callable(updates.verify)
        self.upload_hypertree = Stage.from_callable(updates.upload_hypertree)


@dataclass(frozen=True, slots=True)
class Plan:
    pipeline: Pipeline
    context: Mapping[str, object]       # Mapping, not dict: dict is invariant, so dict[str, str] wouldn't fit


# Op             Steps                                                     Finally   Context
# REATTACH       reattach, wait_for_version (when), stop (when)            –         {}
# UPDATE         download, clear_hypertree (non-critical),                 –         artifact, hypertree_get_url,
#                fetch_hypertree (non-critical), reload, verify (retries=1),         hypertree_upload_url
#                upload_hypertree (non-critical)
# FORCE_UPDATE   download                                                  –         artifact
# START          start, wait_for_log                                       –         {}
# CRASH_RESTART  stop, start, wait_for_log                                 –         {}
# RESTART        stop, start, wait_for_log                                 –         {}
# STOP           stop                                                      –         {}
#
# artifact = artifact_url, artifact_sha256 and version. start produces log_offset for wait_for_log.
# start, wait_for_log and wait_for_version have retries=0: a spawn must not be repeated, and the
# waits kill the process on timeout, so a retry can't succeed. A failed start ends as a crash
# (dead pid in STARTING), so no recipe has a finally step. The hypertree steps only speed loading
# up, so they are non-critical: their failures stay in the step log and never fail the update.

def plan(op: OpType, desired: DesiredState, process: ProcessManager, updates: UpdateManager) -> Plan:
    s = _Stages(process, updates)
    match op:
        case OpType.REATTACH:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.reattach),
                    PipelineStep(s.wait_for_version, retries=0, when=WAIT_FOR_START),
                    PipelineStep(s.stop, when=WAIT_FOR_STOP),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {})
        case OpType.UPDATE:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.download),
                    PipelineStep(s.clear_hypertree, critical=False),
                    PipelineStep(s.fetch_hypertree, critical=False),
                    PipelineStep(s.reload),
                    PipelineStep(s.verify, retries=1),
                    PipelineStep(s.upload_hypertree, critical=False),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {
                "artifact_url": desired.artifact_url,
                "artifact_sha256": desired.artifact_sha256,
                "version": desired.version,
                "hypertree_get_url": desired.hypertree_get_url,
                "hypertree_upload_url": desired.hypertree_upload_url,
            })
        case OpType.FORCE_UPDATE:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.download),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {
                "artifact_url": desired.artifact_url,
                "artifact_sha256": desired.artifact_sha256,
                "version": desired.version,
            })
        case OpType.START:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.start, retries=0),
                    PipelineStep(s.wait_for_log, retries=0),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {})
        case OpType.CRASH_RESTART:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.stop),
                    PipelineStep(s.start, retries=0),
                    PipelineStep(s.wait_for_log, retries=0),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {})
        case OpType.RESTART:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.stop),
                    PipelineStep(s.start, retries=0),
                    PipelineStep(s.wait_for_log, retries=0),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {})
        case OpType.STOP:
            pipeline = Pipeline(
                steps=[
                    PipelineStep(s.stop),
                ],
                finally_steps=[],
            )
            return Plan(pipeline, {})
