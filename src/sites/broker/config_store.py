"""Active compiled config and environment settings per app environment."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppRuntime:
    app_env_id: str
    version: int
    digest: str
    config: dict
    settings: dict


class ConfigStore:
    def active(self, app_env_id: str) -> AppRuntime | None:
        raise NotImplementedError

    def by_host(self, host: str) -> str | None:
        raise NotImplementedError

    def secret(self, app_env_id: str, name: str) -> str | None:
        raise NotImplementedError
