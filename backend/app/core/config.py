"""Application settings, loaded from environment variables (and `.env` in local dev)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "GRI KPI Platform"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True  # set false locally for human-readable console output

    # The app connects as a member of the `gri_app` role, which is subject to row-level security.
    database_url: str = "postgresql+psycopg://gri_api:gri_api@localhost:55432/gri"
    # Migrations run as the schema owner. Falls back to database_url when unset.
    migration_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minio"
    s3_secret_key: SecretStr = SecretStr("minio-dev-password")
    s3_bucket: str = "gri-kpi"

    session_ttl_minutes: int = Field(default=720, gt=0)  # lifetime of a login token

    health_check_timeout_seconds: float = Field(default=2.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
