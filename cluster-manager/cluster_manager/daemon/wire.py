from enum import StrEnum, auto
from ipaddress import IPv4Address

from pydantic import AwareDatetime, BaseModel, ConfigDict


class OpType(StrEnum):
    REATTACH = auto()
    UPDATE = auto()
    FORCE_UPDATE = auto()
    START = auto()
    CRASH_RESTART = auto()
    RESTART = auto()
    STOP = auto()


class ProcessStatus(StrEnum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    CRASHED = auto()


class AgentStatus(StrEnum):
    IDLE = auto()
    BUSY = auto()
    CRASHED_HOLD = auto()


class DesiredState(BaseModel):                  # the heartbeat response body
    model_config = ConfigDict(frozen=True, extra="ignore")
    generation: int
    restart_generation: int
    force_update_generation: int
    desired_running: bool
    version: str                                # also the file server's job id
    artifact_url: str
    artifact_sha256: str
    hypertree_get_url: str                      # the file server's hypertree URL for `version` (GET)
    hypertree_upload_url: str                   # the same URL today (PUT)


class StepView(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    ok: bool
    attempts: int
    error: str | None


class OperationView(BaseModel):                 # reported as current_operation and last_operation
    model_config = ConfigDict(frozen=True)
    op_id: str
    generation: int                             # the desired generation the operation started from
    type: OpType
    started_at: AwareDatetime                   # timezone required; the agent sends UTC
    finished_at: AwareDatetime | None           # timezone required; None while running
    steps: list[StepView]
    failure: str | None


class HeartbeatReport(BaseModel):
    ipv4: IPv4Address                           # the agent's identity, from config.ipv4; a string on the wire
    acknowledged_generation: int
    handled_restart_generation: int
    handled_force_update_generation: int
    failed_generation: int | None
    running_version: str | None
    process_status: ProcessStatus | None        # None while not attached; CRASHED includes hung
    healthy: bool | None                        # HealthMonitor
    violations: list[str] | None                # PerformanceMonitor: None = no verdict, [] = ok
    metrics_available: bool | None              # PerformanceMonitor: None = no data or inactive,
                                                # False = max_failures failed metrics() calls in a row
    agent_status: AgentStatus
    current_operation: OperationView | None
    last_operation: OperationView | None
