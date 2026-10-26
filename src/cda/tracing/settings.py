"""Opt-in tracing configuration with redacted credentials."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TracingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LANGFUSE_", env_file=".env", extra="ignore")

    host: str = ""
    public_key: SecretStr = Field(default=SecretStr(""), repr=False)
    secret_key: SecretStr = Field(default=SecretStr(""), repr=False)

    @property
    def enabled(self) -> bool:
        return bool(
            self.host and self.public_key.get_secret_value() and self.secret_key.get_secret_value()
        )
