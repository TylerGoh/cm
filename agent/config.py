"""The agent's deployment settings, read only from the .env file next to this module.

Imported as `from config import config`. Provisioning (Foreman) writes the file; the process
environment is ignored, so a stray HOSTNAME or PORT can't override a value. No defaults and no
unknown keys: a missing or misspelled setting fails startup. The values are the owner's."""

from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr
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
    ipv4: IPv4Address
    hostname: str
    cluster_name: str
    intf: Annotated[list[str], Field(min_length=1)]

    # server (__main__); it always binds 0.0.0.0
    port: int
    log_level: str
    log_format: str

    # stores
    memory_store_path: Path
    process_store_path: Path

    # worker process (ProcessManager)
    worker_command: Annotated[list[str], Field(min_length=1)]
    worker_console_log_dir: Path
    worker_program_log_path: Path
    worker_initialize_flag: str
    start_timeout: float
    stop_timeout: float

    # worker HTTP (WorkerClient)
    worker_url: str
    worker_timeout: float
    reload_timeout: float

    # artifacts and hypertrees (UpdateManager)
    download_dir: Path
    bundle_dir: Path
    etc_dir: Path
    bundle_symlink_mapping: dict[Path, Path]
    bundle_symlink_default_dir: str
    download_timeout: float
    hypertree_dir: Path
    hypertree_timeout: float
    verify_timeout: float
    verify_interval: float

    # monitors and exporter
    health_interval: float
    max_failures: int
    performance_interval: float
    performance_window_seconds: float
    max_metrics_failures: int
    inventory_interval: float
    require_running: bool
    metrics_export_interval: float

    # cluster manager
    cluster_manager_url: str
    heartbeat_interval: float

    # elastic
    elastic_url: str
    elastic_cert_path: Path
    elastic_api_key: SecretStr
    elastic_metrics_index: str
    elastic_host_index: str
    elastic_timeout: float

    # BEGIN generated: inventory settings
    # END generated: inventory settings


if not ENV_FILE.exists():
    raise FileNotFoundError(f"agent settings file missing: {ENV_FILE}")   # otherwise every field is reported missing
config = AgentConfig()
