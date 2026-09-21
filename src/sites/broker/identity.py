"""Who is calling the Broker: an app workload, and optionally the end user it acts for."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppCaller:
    app_env_id: str
    site: str | None


@dataclass(frozen=True)
class EndUser:
    id: str
    org: str | None
    role: str
    plan: str
    env: str
