"""Configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _clean_domain(value: str) -> tuple[str, str]:
    raw = value.strip().rstrip("/")
    scheme = "https"
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            scheme = prefix[:-3]
            raw = raw[len(prefix) :]
    if not raw or "/" in raw:
        raise ConfigError(f"AUTH0_DOMAIN should look like 'tenant.eu.auth0.com', got {value!r}")
    return scheme, raw


@dataclass(frozen=True)
class Config:
    domain: str
    client_id: str
    client_secret: str
    redirect_uri: str
    secret_key: str
    github_token: str = ""
    github_api: str = "https://api.github.com"
    github_repo: str = ""
    approval_base: str = "http://localhost:4100"
    approval_ttl: int = 600
    scope: str = "openid profile email"
    scheme: str = "https"

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.domain}"

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        source = os.environ if env is None else env
        missing = [
            name
            for name in ("AUTH0_DOMAIN", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET", "AUTH0_SECRET")
            if not source.get(name, "").strip()
        ]
        if missing:
            raise ConfigError(
                "set " + ", ".join(missing) + " (see .env.example) before starting the app"
            )
        redirect = source.get("AUTH0_REDIRECT_URI", "http://localhost:3000/callback").strip()
        if not redirect.startswith(("http://", "https://")):
            raise ConfigError("AUTH0_REDIRECT_URI must be an absolute URL")
        approval = source.get("APPROVAL_BASE", "http://localhost:4100").strip().rstrip("/")
        if not approval.startswith(("http://", "https://")):
            raise ConfigError("APPROVAL_BASE must be an absolute URL")
        scheme, domain = _clean_domain(source["AUTH0_DOMAIN"])
        return cls(
            domain=domain,
            client_id=source["AUTH0_CLIENT_ID"].strip(),
            client_secret=source["AUTH0_CLIENT_SECRET"].strip(),
            redirect_uri=redirect,
            secret_key=source["AUTH0_SECRET"].strip(),
            github_token=source.get("GITHUB_TOKEN", "").strip(),
            github_api=source.get("GITHUB_API", "https://api.github.com").strip().rstrip("/"),
            github_repo=source.get("GITHUB_REPO", "").strip().strip("/"),
            approval_base=approval,
            approval_ttl=int(source.get("APPROVAL_TTL", "600")),
            scheme=scheme,
        )
