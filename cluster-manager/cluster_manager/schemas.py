"""The contract between the CLI (cluster_manager/client) and the daemon, as wire.py is between
the agent and the daemon. One request/response pair per endpoint. Responses are views built for
display, not database rows, so the tables can change without breaking the client. One standard,
no versioning: the client and the daemon are deployed together. Every timestamp is AwareDatetime.
The status enums the client shows (DeploymentStatus, IncidentKind, IncidentSource) are defined here; the
daemon's database models import them, so both sides use one definition."""

from enum import StrEnum, auto

from pydantic import AwareDatetime, BaseModel, ConfigDict

from cluster_manager.daemon.wire import AgentStatus, OperationView, OpType, ProcessStatus


class DeploymentStatus(StrEnum):
    """A version's status in version_deployments (see architecture.md, Rules for the orchestrator)."""
    VERIFYING = auto()
    ACTIVE = auto()
    STABLE = auto()
    UNSTABLE = auto()


class IncidentKind(StrEnum):
    NON_RESPONDING = auto()         # opened by the watcher, from silence
    METRICS_UNAVAILABLE = auto()    # opened by the heartbeat service, from metrics_available=False
    ARTIFACT_MISMATCH = auto()      # opened by the file watch, from a re-fetched artifact's sha256


class IncidentSource(StrEnum):
    """Who raised the incident."""
    AGENT = auto()
    CLUSTER_MANAGER = auto()


class AgentCondition(StrEnum):
    """One label per agent, derived by the daemon from desired + observed state.
    Checked in this order; the first that applies wins."""
    NON_RESPONDING = auto()     # no heartbeat within the non-responding threshold
    CRASHED_HOLD = auto()       # used its crash restart: needs a force bump or a human
    FAILED = auto()             # gave up on the current generation
    UPDATING = auto()           # an operation is running
    PENDING = auto()            # hasn't acknowledged the current generation yet
    CRASHED = auto()            # crashed, its one crash restart still to come
    STOPPED = auto()            # stopped, as desired
    CONVERGED = auto()          # running the desired version, as desired
    DRIFTING = auto()           # none of the above: not where it should be and nothing running


class CanaryOutcome(StrEnum):
    """A canary agent's outcome, in the order they are checked (see architecture.md, Canary)."""
    FAULTY_UPDATE = auto()
    CRASHED = auto()
    NOT_CONVERGED = auto()
    NO_VERDICT = auto()
    PERFORMANCE = auto()
    PASSED = auto()


# --- shared pieces ---

class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class AgentRow(_View):
    """One line of `cm agents`."""
    ip: str
    condition: AgentCondition
    desired_version: str
    running_version: str | None
    desired_running: bool
    canary: bool                                 # its desired version is the one being verified
    process_status: ProcessStatus | None
    agent_status: AgentStatus
    healthy: bool | None
    violations: list[str] | None
    metrics_available: bool | None
    last_heartbeat_at: AwareDatetime
    current_operation: OpType | None


class DesiredChange(_View):
    """One desired-state history row: what a generation asked for."""
    generation: int
    version: str
    desired_running: bool
    restart_generation: int
    force_update_generation: int
    changed_at: AwareDatetime


class IncidentRow(_View):
    id: int
    source: IncidentSource
    ip: str
    version: str | None
    kind: IncidentKind
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None
    notified: bool
    recovery_notified: bool


# --- GET /status ---

class CanarySummary(_View):
    version: str
    deadline: AwareDatetime
    agents: int
    counts: dict[CanaryOutcome, int]


class StatusResponse(_View):
    active_version: str
    verifying_version: str | None
    canary: CanarySummary | None
    agents: int                                  # live agents
    conditions: dict[AgentCondition, int]        # only conditions with at least one agent
    open_incidents: int


# --- GET /agents, GET /agents/{ip} ---

class AgentListResponse(_View):
    agents: list[AgentRow]


class AgentDetailResponse(_View):
    agent: AgentRow
    registered_at: AwareDatetime
    generation: int
    acknowledged_generation: int
    failed_generation: int | None
    restart_generation: int
    handled_restart_generation: int
    force_update_generation: int
    handled_force_update_generation: int
    current_operation: OperationView | None
    last_operation: OperationView | None
    recent_operations: list[OperationView]       # newest first, from the operations table
    history: list[DesiredChange]                 # newest first
    open_incidents: list[IncidentRow]


# --- GET /versions ---

class VersionRow(_View):
    version: str
    status: DeploymentStatus
    created_at: AwareDatetime
    verified_at: AwareDatetime | None
    activated_by_rollback: bool
    unstable_reason: str | None
    agents: int                                  # live agents whose desired version is this one


class VersionListResponse(_View):
    versions: list[VersionRow]                   # newest first


# --- GET /canary ---

class CanaryAgent(_View):
    ip: str
    outcome: CanaryOutcome                       # as of now; only the deadline counts


class CanaryLimits(_View):
    max_crashes: int
    max_violations: int
    max_not_converged: int


class CanaryView(_View):
    version: str
    created_at: AwareDatetime
    deadline: AwareDatetime
    agents: list[CanaryAgent]
    counts: dict[CanaryOutcome, int]
    limits: CanaryLimits
    would_reject: bool                           # if judged now


class CanaryResponse(_View):
    canary: CanaryView | None                    # None: no version is VERIFYING


# --- GET /incidents ---

class IncidentListResponse(_View):
    open: list[IncidentRow]
    recently_closed: list[IncidentRow]


# --- actions ---

class DesiredStateChangeResponse(_View):
    """The answer to every per-agent action: the new desired state, under its new generation."""
    ip: str
    desired: DesiredChange


class DecommissionResponse(_View):
    ip: str
    decommissioned_at: AwareDatetime


class DispatchRequest(_View):
    version: str                                 # exactly what the worker's /version will return
    upstream_url: str                            # where the file server fetches the artifact from


class DispatchResponse(_View):
    version: str
    canary_agents: list[str]
    deadline: AwareDatetime


class RollbackResponse(_View):
    from_version: str
    to_version: str
    agents: int                                  # live agents pointed at to_version


class ErrorResponse(_View):
    """Body of every 4xx the daemon returns on purpose."""
    detail: str
