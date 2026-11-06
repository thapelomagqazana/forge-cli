"""Configuration management."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FORGE_", env_file=".env", extra="ignore")

    log_level: str = "INFO"
    blueprint_dir: str = ".forge/blueprints"


settings = Settings()
