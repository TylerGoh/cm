"""The daemon's deployment settings, read only from .env in the project folder.

Imported as `from cluster_manager.daemon.config import config`. Same rules as the agent's config:
the process environment is ignored, there are no defaults and no unknown keys, so a missing or
misspelled setting fails startup. Durations are plain seconds."""

from datetime import timedelta
from pathlib import Path

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"     # the project folder


class DaemonConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="forbid", frozen=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, dotenv_settings)          # init_settings lets tests pass values directly

    # server; it always binds 0.0.0.0
    port: int
    log_level: str
    log_format: str

    # database
    database_url: str                    # sqlite+aiosqlite:////path/to/cluster_manager.db

    # file server
    file_server_url: str
    file_server_timeout: float
    file_server_fetch_timeout: float     # dispatch and bootstrap wait for the fetch from upstream

    # bootstrap: used on the daemon's first boot only
    bootstrap_version: str
    bootstrap_upstream_url: str

    # agents
    non_responding_seconds: float        # 3 ticks (30) by design

    # canary
    canary_size: int
    canary_duration_seconds: float       # must comfortably exceed an update plus one 5-minute window
    canary_max_crashes: int
    canary_max_violations: int
    canary_max_not_converged: int

    # the ACTIVE fleet after promotion: the canary's buckets with higher limits, judged every tick
    fleet_max_crashes: int
    fleet_max_violations: int
    fleet_max_not_converged: int
    fleet_breach_seconds: float          # over a limit this long rolls back automatically; 0 = at once

    # notifications
    mattermost_webhook_url: str          # empty: nothing is sent, incidents stay pending
    mattermost_timeout: float

    @property
    def non_responding_after(self) -> timedelta:
        return timedelta(seconds=self.non_responding_seconds)

    @property
    def canary_duration(self) -> timedelta:
        return timedelta(seconds=self.canary_duration_seconds)

    @property
    def fleet_breach_duration(self) -> timedelta:
        return timedelta(seconds=self.fleet_breach_seconds)


if not ENV_FILE.exists():
    raise FileNotFoundError(f"daemon settings file missing: {ENV_FILE}")
config = DaemonConfig()
