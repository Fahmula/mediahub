"""Application settings, read from ``MEDIAHUB_*`` environment variables."""

from __future__ import annotations

from enum import StrEnum
from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class CookieSecure(StrEnum):
    AUTO = "auto"
    TRUE = "true"
    FALSE = "false"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDIAHUB_", extra="ignore")

    config_dir: Path = Path("/config")
    host: str = "0.0.0.0"  # noqa: S104 - container default, bind address is configurable
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    cookie_secure: CookieSecure = CookieSecure.AUTO
    session_days: int = Field(default=30, ge=1, le=365)
    demo_mode: bool = False
    # Display name used on first start (editable later in Settings → General).
    server_name: str | None = None

    # Optional overrides. When unset, a key is generated into config_dir/secret.key.
    secret_key: SecretStr | None = None
    # Optional headless bootstrap of the admin account (only used while no user exists).
    admin_user: str | None = None
    admin_password: SecretStr | None = None

    # Proxy headers (X-Forwarded-For/Proto) are trusted only from these addresses.
    trusted_proxies: str = "127.0.0.1"

    # Contact sent to browser push services with each notification (a URL or mailto:).
    push_contact: str = "https://github.com/Fahmula/mediahub"

    @cached_property
    def db_path(self) -> Path:
        return self.config_dir / "mediahub.db"

    @cached_property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @cached_property
    def secret_key_path(self) -> Path:
        return self.config_dir / "secret.key"

    @cached_property
    def lock_path(self) -> Path:
        return self.config_dir / "mediahub.lock"

    @cached_property
    def cache_dir(self) -> Path:
        return self.config_dir / "cache"
