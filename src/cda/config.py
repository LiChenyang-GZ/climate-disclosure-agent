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

    # Only their presence is checked; each SDK reads its own key or settings itself (D90).
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    google_genai_use_vertexai: bool = False
    google_cloud_project: str | None = None
    # D83: scripted fake model for tests and local smoke runs, never for real checks.
    cda_fake_llm: bool = False

    # D84: Microsoft Entra app (public client, device-code flow); no secret is needed.
    cda_ms_client_id: str | None = None
    cda_ms_tenant: str = "organizations"
    # D85: Salesforce connected app (client credentials flow). The settings page can
    # replace these in API memory only; they are never stored or logged.
    cda_sf_instance_url: str | None = None
    cda_sf_client_id: str | None = None
    cda_sf_client_secret: SecretStr | None = None
    # Optional web origin (e.g. http://localhost:3101) that makes the report link in a
    # Case absolute; without it the Case carries the path /runs/{id}/report.
    cda_web_url: str | None = None

    def llm_ready_for(self, provider: str) -> bool:
        def present(value: SecretStr | None) -> bool:
            return bool(value and value.get_secret_value().strip())

        if self.cda_fake_llm:
            return True
        if provider == "gemini":
            vertex = self.google_genai_use_vertexai and bool(
                (self.google_cloud_project or "").strip()
            )
            return vertex or present(self.gemini_api_key) or present(self.google_api_key)
        return present(self.openai_api_key)

    @property
    def llm_ready(self) -> bool:
        """The default provider's key (OpenAI); kept for callers without a model key."""
        return self.llm_ready_for("openai")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


KEY_MISSING = {
    "openai": "OpenAI API key isn't set. Add OPENAI_API_KEY to .env and restart the backend.",
    "gemini": (
        "Gemini API key isn't set. Add GEMINI_API_KEY (or the Vertex AI settings) to .env "
        "and restart the backend."
    ),
}


def provider_of(model_key: str) -> str:
    """The provider of a configured model key; unknown keys count as OpenAI, and are
    rejected later by the run's own validation."""
    from cda.llm.cost import ModelTable

    models = ModelTable.load().root
    return models[model_key].provider if model_key in models else "openai"
