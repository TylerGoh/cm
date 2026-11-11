from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import TypedDict

from pydantic import AwareDatetime, BaseModel, ConfigDict

from agent.wire import ProcessStatus


@dataclass(frozen=True, slots=True)
class HostInfo:
    """This box's identity as provisioned upstream (Foreman), read from config once at startup.
    Nothing in the agent looks it up from the OS."""
    ipv4: IPv4Address
    hostname: str
    cluster_name: str                            # sent in every Elastic document


@dataclass(frozen=True, slots=True)
class Observed:                                  # runtime, one per tick
    process_status: ProcessStatus | None         # None = not attached yet; CRASHED includes hung
    healthy: bool | None                         # read once per tick, for the report; decide must not use it
    running_version: str | None                  # from WorkerClient.version(); only while RUNNING


class AgentMemory(BaseModel):                    # persisted, policy memory only
    model_config = ConfigDict(frozen=True)
    acknowledged_generation: int
    handled_restart_generation: int              # per-agent counters, 0 on first boot
    handled_force_update_generation: int
    failed_generation: int | None
    crash_restarted: bool


class AgentMemoryChanges(TypedDict, total=False):
    """Keyword arguments of Supervisor._set_memory. Mirrors AgentMemory (tests/test_models.py checks it).
    A missing key leaves that field unchanged."""
    acknowledged_generation: int
    handled_restart_generation: int
    handled_force_update_generation: int
    failed_generation: int | None
    crash_restarted: bool


class ProcessMemory(BaseModel):                  # persisted; the derived status is ProcessStatus
    model_config = ConfigDict(frozen=True)
    pid: int | None
    create_time: AwareDatetime | None            # the process's creation time from the OS, in UTC
    state: ProcessStatus                         # never CRASHED
