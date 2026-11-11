from dataclasses import dataclass

from agent.wire import DesiredState, HeartbeatReport, ProcessStatus


@dataclass(frozen=True, slots=True)
class Conditions:
    """What the supervisor tells the monitors and the exporter about the world, once per tick."""
    process_status: ProcessStatus | None    # None while not attached; CRASHED includes hung
    busy: bool                              # any operation is running
    updating: bool                          # the running operation is an update
    running_version: str | None             # only fetched while RUNNING
    desired: DesiredState | None = None     # the last desired state accepted; None before the first
    observed: HeartbeatReport | None = None # the agent's state as its heartbeat reports it


class ConditionFeed:
    """The supervisor publishes; the monitors and the exporter read. Breaks the construction cycle."""

    def __init__(self) -> None:
        self._current = Conditions(process_status=None, busy=False, updating=False,
                                   running_version=None)

    def publish(self, conditions: Conditions) -> None:
        self._current = conditions

    def current(self) -> Conditions:
        return self._current
