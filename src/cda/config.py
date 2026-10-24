from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Climate Disclosure Agent"
    database_url: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
