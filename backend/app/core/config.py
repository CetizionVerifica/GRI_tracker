"""Application settings, loaded from environment variables (and `.env` in local dev)."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "GRI KPI Platform"

    database_url: str = "postgresql+psycopg://gri:gri@localhost:55432/gri"
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minio"
    s3_secret_key: SecretStr = SecretStr("minio-dev-password")
    s3_bucket: str = "gri-kpi"


@lru_cache
def get_settings() -> Settings:
    return Settings()
