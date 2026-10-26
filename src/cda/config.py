import os
import socket
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Climate Disclosure Agent"
    database_url: str | None = None
    cda_upload_dir: Path = Path("uploads")
    cda_max_upload_mb: int = Field(default=50, gt=0)

    worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    lease_seconds: int = 90
    heartbeat_seconds: int = 30
    poll_seconds: float = 5.0
    notify_channel: str = "cda_runs"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
