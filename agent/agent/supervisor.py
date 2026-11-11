import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv4Address
from typing import Unpack

from agent.cluster_manager_client import ClusterManagerClient
from agent.decide import decide
from agent.feed import ConditionFeed, Conditions
from agent.health import HealthMonitor
from agent.performance import PerformanceMonitor
from agent.models import AgentMemory, AgentMemoryChanges, Observed
from agent.operations import plan
from agent.pipeline import Pipeline
from agent.process import ProcessManager
from agent.state_store import JsonStore
from agent.updates import UpdateManager
from agent.wire import (
    AgentStatus, DesiredState, HeartbeatReport, OperationView, OpType, ProcessStatus, StepView,
)
from agent.worker_client import WorkerClient

log = logging.getLogger("agent.supervisor")

SHUTDOWN_GRACE = 20.0       # seconds for finally steps; part of the ~40s systemd TimeoutStopSec
UPDATING_OPS = (OpType.UPDATE, OpType.FORCE_UPDATE)


@dataclass(frozen=True, slots=True)
class ActiveOperation:
    """Everything about the running operation, in one flat record."""
    op_id: str
    type: OpType
    started_at: datetime
    desired: DesiredState               # what it was started from
    pipeline: Pipeline                  # its steps and step log
    task: asyncio.Task[None]


def _operation_view(active: ActiveOperation, *, finished_at: datetime | None,
                    failure: str | None) -> OperationView:
    """Module function so that wire never imports pipeline."""
    return OperationView(
        op_id=active.op_id,
        generation=active.desired.generation,
        type=active.type,
        started_at=active.started_at,
        finished_at=finished_at,
        steps=[StepView(name=r.name, ok=r.ok, attempts=r.attempts, error=r.error)
               for r in active.pipeline.step_log],
        failure=failure,
    )


class Supervisor:
    def __init__(self, ipv4: IPv4Address, process: ProcessManager, updates: UpdateManager,
                 worker: WorkerClient, health: HealthMonitor, performance: PerformanceMonitor,
                 feed: ConditionFeed, cluster_manager: ClusterManagerClient,
                 memory_store: JsonStore[AgentMemory]) -> None:
        self._ipv4 = ipv4                       # the agent's identity, sent in every report
        self.process = process
        self.updates = updates
        self._worker = worker                   # read only: .version() in _observe
        self._health = health                   # read only: .healthy, once per tick in _observe
        self._performance = performance         # read only: .violations and .metrics_available
        self._feed = feed                       # write only: _publish
        self._cluster_manager = cluster_manager
        self._memory_store = memory_store
        self.memory = memory_store.load()       # first boot is handled in app.py before this
        self.last_operation: OperationView | None = None  # in memory only
        self._active: ActiveOperation | None = None
        self._last_observed: Observed | None = None
        self._last_desired: DesiredState | None = None   # kept through failed or stale heartbeats
        self._sync_failures = 0

    async def heartbeat_tick(self) -> None:
        observed = await self._observe()
        self._last_observed = observed
        desired = await self._sync(observed)
        if not self._active and desired is not None:
            op = decide(desired, observed, self.memory)
            if op is not None:
                self._start_operation(op, desired)
        self._publish(observed)

    # --- observe / publish ---

    async def _observe(self) -> Observed:
        """Gathers one snapshot. The status rules live in ProcessManager.derive_status."""
        healthy = self._health.healthy                   # the only read this tick, before any await
        status = self.process.derive_status(healthy) if self.process.attached else None
        return Observed(
            process_status=status,
            healthy=healthy,
            running_version=await self._worker.version() if status is ProcessStatus.RUNNING else None,
        )

    def _publish(self, observed: Observed) -> None:
        active = self._active
        self._feed.publish(Conditions(
            process_status=observed.process_status,
            busy=active is not None,
            updating=active is not None and active.type in UPDATING_OPS,
            running_version=observed.running_version,
            desired=self._last_desired,
            observed=self._report(observed),             # built now, so a finished operation shows
        ))

    # --- heartbeat ---

    async def _sync(self, observed: Observed) -> DesiredState | None:
        try:
            desired = await self._cluster_manager.heartbeat(self._report(observed))
        except Exception:
            self._sync_failures += 1
            if self._sync_failures == 1:
                log.exception("heartbeat failed")        # only the first failure is logged
            return None
        if self._sync_failures:
            log.info("heartbeat recovered after %d failures", self._sync_failures)
            self._sync_failures = 0
        acknowledged = self.memory.acknowledged_generation
        if desired.generation < acknowledged:
            return None                                  # stale
        if desired.generation > acknowledged:
            self._set_memory(acknowledged_generation=desired.generation)
        self._last_desired = desired
        return desired

    def _report(self, observed: Observed) -> HeartbeatReport:
        m = self.memory
        return HeartbeatReport(
            ipv4=self._ipv4,
            acknowledged_generation=m.acknowledged_generation,
            handled_restart_generation=m.handled_restart_generation,
            handled_force_update_generation=m.handled_force_update_generation,
            failed_generation=m.failed_generation,
            running_version=observed.running_version,
            process_status=observed.process_status,
            healthy=observed.healthy,                    # the same reading the status was derived from
            violations=self._performance.violations,
            metrics_available=self._performance.metrics_available,
            agent_status=self._agent_status(observed),
            current_operation=(_operation_view(self._active, finished_at=None, failure=None)
                               if self._active else None),
            last_operation=self.last_operation,
        )

    def _agent_status(self, observed: Observed) -> AgentStatus:
        if self._active:
            return AgentStatus.BUSY
        if observed.process_status is ProcessStatus.CRASHED and self.memory.crash_restarted:
            return AgentStatus.CRASHED_HOLD
        return AgentStatus.IDLE

    # --- operations ---

    def _start_operation(self, op: OpType, desired: DesiredState) -> None:
        recipe = plan(op, desired, self.process, self.updates)   # first: a plan() error spends nothing
        if op is OpType.CRASH_RESTART:
            self._set_memory(crash_restarted=True)               # before the task: never runs unrecorded
        op_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        task = asyncio.create_task(self._drive(recipe.context))
        self._active = ActiveOperation(op_id=op_id, type=op, started_at=started_at,
                                       desired=desired, pipeline=recipe.pipeline, task=task)

    async def _drive(self, context: Mapping[str, object]) -> None:
        # Runs only after _start_operation returns, so self._active is already set.
        active = self._active
        assert active is not None
        log.info("operation %s (%s) starting, context=%r", active.op_id, active.type, dict(context))
        try:
            await active.pipeline.run(context)
            failure = active.pipeline.failure
        except Exception as e:                           # e.g. _check_context rejected the context
            if active.pipeline.failure is not None:
                failure = active.pipeline.failure
            else:
                failure = f"{type(e).__name__}: {e}"
        for record in active.pipeline.step_log:
            if not record.ok:
                log.warning("step %s failed after %d attempts: %s",
                            record.name, record.attempts, record.error)
        self._finish(active, failure)

    def _finish(self, active: ActiveOperation, failure: str | None) -> None:
        view = _operation_view(active, finished_at=datetime.now(UTC), failure=failure)
        self.last_operation = view                       # every op, including REATTACH
        log.info("operation finished: %s", view.model_dump_json())

        op, desired = active.type, active.desired
        changes: AgentMemoryChanges = {}
        if failure is None:
            if op is OpType.RESTART:
                changes["handled_restart_generation"] = desired.restart_generation
            if op in (OpType.UPDATE, OpType.FORCE_UPDATE):
                changes["crash_restarted"] = False
        elif op is not OpType.REATTACH:                  # REATTACH changes no memory
            changes["failed_generation"] = desired.generation
        if op is OpType.FORCE_UPDATE:                    # handled whether it succeeded or not
            changes["handled_force_update_generation"] = desired.force_update_generation
        if changes:
            self._set_memory(**changes)

        self._active = None
        if self._last_observed is not None:
            self._publish(self._last_observed)

    # --- memory / shutdown ---

    def _set_memory(self, **changes: Unpack[AgentMemoryChanges]) -> None:   # the only writer
        """Saves first, so a failed save leaves both the file and memory on the old value."""
        new_memory = self.memory.model_copy(update=changes)
        self._memory_store.save(new_memory)
        self.memory = new_memory

    async def shutdown(self) -> None:
        """Cancels the active operation; an interrupted operation is discarded, not finished."""
        active = self._active
        if active is None:
            return
        active.task.cancel()                             # finally steps run on cancellation
        _, pending = await asyncio.wait({active.task}, timeout=SHUTDOWN_GRACE)
        if pending:
            log.warning("finally steps did not finish within %.0fs", SHUTDOWN_GRACE)
            active.task.cancel()                         # second cancel stops the remaining ones
