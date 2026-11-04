"""The agent's deployment settings, read only from the .env file next to this module.

Imported as `from config import config`. Provisioning (Foreman) writes the file; the process
environment is ignored, so a stray HOSTNAME or PORT can't override a value. No defaults and no
unknown keys: a missing or misspelled setting fails startup. The values are the owner's; the
comments record the constraints the design depends on."""

from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent / ".env"     # always next to this module, whatever the cwd


class AgentConfig(BaseSettings):
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

    # host identity, set upstream by Foreman
    ipv4: IPv4Address                    # the agent's identity in every heartbeat
    hostname: str
    location: str

    # server (__main__); it always binds 0.0.0.0
    port: int
    log_level: str
    log_format: str

    # stores
    memory_store_path: Path
    process_store_path: Path

    # worker process (ProcessManager)
    worker_command: Annotated[list[str], Field(min_length=1)]   # JSON list: binary first, then each flag
    worker_console_log_dir: Path         # stdout/stderr, one file per spawn named by the spawn time (UTC)
    worker_program_log_path: Path        # the worker's own log; wait_for_log reads its ready line
    worker_initialize_flag: str          # text that marks the ready line in the worker's own log
    start_timeout: float                 # bounds wait_for_log and wait_for_version
    stop_timeout: float                  # bounds the wait for the exit after a kill

    # worker HTTP (WorkerClient)
    worker_url: str
    worker_timeout: float                # every call except the reload phases; keep it short
    reload_timeout: float                # each reload phase

    # artifacts and hypertrees (UpdateManager)
    download_dir: Path                   # temporary zips
    bundle_dir: Path                     # "update_bundle": one unpacked folder per version
    etc_dir: Path
    bundle_links: dict[Path, Path]       # JSON object: file inside a bundle -> file path under etc_dir (a symlink)
    download_timeout: float              # fetching and unpacking one bundle
    hypertree_dir: Path                  # where the worker reads and generates hypertrees
    hypertree_timeout: float             # each hypertree step
    verify_timeout: float
    verify_interval: float

    # monitors and exporter
    health_interval: float
    max_failures: int                    # max_failures × health_interval must outlast a stall under load
    performance_interval: float
    performance_window_seconds: float    # canary_duration on the daemon must allow for one window
    max_failed_ratio: float
    max_metrics_failures: int
    inventory_interval: float            # at least one tick (10s); shorter only sends duplicates
    require_running: bool
    metrics_export_interval: float       # seconds between metrics documents to Elastic

    # cluster manager
    cluster_manager_url: str

    # elastic
    elastic_url: str
    elastic_cert_path: Path              # elastic.cert: the CA certificate that verifies Elastic
    elastic_metrics_index: str
    elastic_host_index: str              # inventory records and the agent_started record
    elastic_timeout: float               # short: the start record is awaited during startup


if not ENV_FILE.exists():
    raise FileNotFoundError(f"agent settings file missing: {ENV_FILE}")   # otherwise every field is reported missing
config = AgentConfig()
