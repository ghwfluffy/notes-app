from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_path_prefix(value: str) -> str:
    trimmed = value.strip()
    if trimmed in {"", "/"}:
        return ""
    prefixed = trimmed if trimmed.startswith("/") else f"/{trimmed}"
    return prefixed.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = "development"
    app_name: str = "My Notes"
    app_base_path: str = "/notes"
    public_url: str = "http://localhost:8096"
    auth_base_url: str = "/auth"
    oauth_server_base_url: str | None = None
    oauth_client_id: str = "notes"
    oauth_scope: str = "openid profile"
    oauth_state_cookie_name: str = "notes_oauth_state"
    session_cookie_name: str = "notes_session"
    session_duration_minutes: int = 1440
    session_key: str = "notes-development-session-key"
    postgres_user: str = "notes"
    postgres_password: str = "notes_dev_password"
    postgres_db: str = "notes"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    agent_integration_token_secret: str = ""
    federated_apps: str = ""

    @field_validator("public_url")
    @classmethod
    def public_url_must_be_origin(cls, value: str) -> str:
        parsed = urlsplit(value.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PUBLIC_URL must be an HTTP origin.")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_URL must contain only the scheme and host.")
        return value.rstrip("/")

    @property
    def normalized_app_base_path(self) -> str:
        return normalize_path_prefix(self.app_base_path)

    @property
    def public_base_url(self) -> str:
        return f"{self.public_url}{self.normalized_app_base_path}"

    @property
    def normalized_auth_base_url(self) -> str:
        value = self.auth_base_url.rstrip("/")
        if value.startswith(("http://", "https://")):
            return value
        return f"{self.public_url}{normalize_path_prefix(value)}"

    @property
    def normalized_oauth_server_base_url(self) -> str:
        value = (self.oauth_server_base_url or self.auth_base_url).rstrip("/")
        if value.startswith(("http://", "https://")):
            return value
        return f"{self.public_url}{normalize_path_prefix(value)}"

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url}/api/v1/auth/oauth/callback"

    @property
    def cookie_secure(self) -> bool:
        return self.app_env == "production"

    @property
    def database_url(self) -> str:
        return (
            "postgresql+psycopg2://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def browser_base_url(self, value: str) -> str:
        raw = value.strip().rstrip("/")
        if raw.startswith(("http://", "https://")):
            return raw
        return normalize_path_prefix(raw)

    @property
    def account_settings_url(self) -> str:
        auth_url = self.browser_base_url(self.auth_base_url)
        return f"{auth_url}?tab=account-settings"

    @property
    def federated_banner_sites(self) -> list[dict[str, str]]:
        if self.federated_apps.strip():
            try:
                parsed = json.loads(self.federated_apps)
            except json.JSONDecodeError as error:
                raise ValueError("FEDERATED_APPS must be valid JSON.") from error
            if not isinstance(parsed, list):
                raise ValueError("FEDERATED_APPS must be a JSON array.")
            sites: list[dict[str, str]] = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    raise ValueError("Each FEDERATED_APPS entry must be an object.")
                slug = entry.get("slug")
                name = entry.get("name")
                base_url = entry.get("baseUrl")
                description = entry.get("description", "")
                if not all(isinstance(value, str) and value.strip() for value in (slug, name, base_url)):
                    raise ValueError("Each FEDERATED_APPS entry needs slug, name, and baseUrl strings.")
                sites.append(
                    {
                        "slug": slug.strip(),
                        "name": name.strip(),
                        "baseUrl": self.browser_base_url(base_url),
                        "description": description.strip() if isinstance(description, str) else "",
                    }
                )
            if sites:
                return sites
        return [
            {
                "slug": "federated-services",
                "name": "Federated Services",
                "baseUrl": self.browser_base_url(self.auth_base_url),
                "description": "Account settings and federated service administration.",
            },
            {
                "slug": "notes",
                "name": "My Notes",
                "baseUrl": self.normalized_app_base_path or "/",
                "description": "Private lists, ideas, quotes, and things to remember.",
            },
        ]


def validate_production(settings: Settings) -> None:
    unsafe: list[str] = []
    if (
        len(settings.session_key) < 32
        or settings.session_key == "notes-development-session-key"
        or settings.session_key.startswith("replace-with-")
    ):
        unsafe.append("SESSION_KEY")
    if settings.postgres_password in {"", "change-me", "notes_dev_password"}:
        unsafe.append("POSTGRES_PASSWORD")
    if (
        len(settings.agent_integration_token_secret) < 32
        or settings.agent_integration_token_secret.startswith("replace-with-")
    ):
        unsafe.append("AGENT_INTEGRATION_TOKEN_SECRET")
    if settings.public_url.startswith("http://"):
        unsafe.append("PUBLIC_URL")
    if unsafe:
        raise ValueError(f"Unsafe production configuration values: {', '.join(unsafe)}")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.app_env == "production":
        validate_production(settings)
    return settings
