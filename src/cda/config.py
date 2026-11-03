import os
import socket
from pathlib import Path

from pydantic import Field, SecretStr
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

    # Only its presence is checked; the OpenAI client reads the key itself.
    openai_api_key: SecretStr | None = None
    # D83: scripted fake model for tests and local smoke runs, never for real checks.
    cda_fake_llm: bool = False

    # D84: Microsoft Entra app (public client, device-code flow); no secret is needed.
    cda_ms_client_id: str | None = None
    cda_ms_tenant: str = "organizations"

    @property
    def llm_ready(self) -> bool:
        return self.cda_fake_llm or bool(
            self.openai_api_key and self.openai_api_key.get_secret_value().strip()
        )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
