from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DOCKER_MARKER = Path("/.dockerenv")
_DEFAULT_DOCKER_DB = "/app/data/family_tasks.sqlite3"


class Settings(BaseSettings):
    bot_token: str
    db_path: str = "./data/family_tasks.sqlite3"
    log_level: str = "INFO"
    alice_webhook_enabled: bool = False
    alice_webhook_host: str = "0.0.0.0"
    alice_webhook_port: int = 8080
    alice_webhook_path: str = "/alice/webhook"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("bot_token", "db_path", "log_level", "alice_webhook_host", "alice_webhook_path", mode="before")
    @classmethod
    def strip_crlf_and_bom(cls, v: object) -> object:
        """Windows CRLF / UTF-8 BOM in .env break Docker env_file and Telegram auth."""
        if isinstance(v, str):
            return v.replace("\ufeff", "").strip().replace("\r", "")
        return v

    @model_validator(mode="after")
    def db_path_must_live_on_app_volume_in_docker(self) -> Self:
        """env_file can inject a host/Windows DB_PATH; SQLite then fails in Linux container."""
        if not _DOCKER_MARKER.exists():
            return self
        normalized = (self.db_path or "").strip().replace("\\", "/")
        if not normalized.startswith("/app/"):
            self.db_path = _DEFAULT_DOCKER_DB
        return self


settings = Settings()
