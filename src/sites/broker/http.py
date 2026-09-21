"""Transport-neutral request/response types and the route table.

Handlers only see these types, so the server underneath can change without touching
them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from sites.broker.config_store import AppRuntime, ConfigStore
from sites.broker.identity import AppCaller, EndUser

# "site" is for the browser-facing sign-in routes on the app's own domain: the app is
# taken from the Host header (or from an app token plus X-Hull-Host when the app backend
# relays). Every "app*" mode requires the app token itself, so browser code on the app
# domain cannot call flows, agents or tools around the app backend's own checks.
AUTH_MODES = frozenset({"none", "site", "app", "app+user", "app+optional_user", "admin"})


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, str]
    headers: Mapping[str, str]
    body: Any
    host: str
    client_ip: str
    cookies: dict[str, str]
    app: AppCaller | None = None
    user: EndUser | None = None
    runtime: AppRuntime | None = None


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class StreamResponse:
    events: Iterator[tuple[str, dict]]
    headers: dict[str, str] = field(default_factory=dict)


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


Handler = Callable[[Request], "Response | StreamResponse"]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    auth: str


_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile(pattern: str) -> re.Pattern[str]:
    # A parameter never spans a slash, and the whole path must match.
    parts, last = [], 0
    for match in _PARAM.finditer(pattern):
        parts.append(re.escape(pattern[last:match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        last = match.end()
    parts.append(re.escape(pattern[last:]))
    return re.compile("^" + "".join(parts) + "$")


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, *, auth: str) -> None:
        if auth not in AUTH_MODES:
            raise ValueError(f"unknown auth mode {auth!r}")
        self._routes.append(Route(method.upper(), _compile(pattern), handler, auth))

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        """Return the route and its path parameters; None when no pattern matches.

        A path that matches a pattern under another method raises 405 instead of 404,
        so a client that used the wrong verb is told so.
        """
        other_method = False
        for route in self._routes:
            found = route.pattern.match(path)
            if found is None:
                continue
            if route.method != method.upper():
                other_method = True
                continue
            return route, found.groupdict()
        if other_method:
            raise HttpError(405, "method_not_allowed", "this path does not accept that method")
        return None


@dataclass
class Services:
    db: Any
    config: ConfigStore
    keys: Any
    clock: Callable[[], float]
    # Everything below this line is a C-side extension beyond the D-facing
    # contract (contract section 4 only promises db/config/keys/clock). D's
    # register() must not rely on these; they exist so C's own route modules
    # (auth.py, identity.py, admin_api.py) can share one Services instance
    # instead of server.py threading extra arguments through every register().
    auth: Any = None
    mailer: Any = None
    reviewer: Any = None
    admin_token: str = ""
    dev_tokens_enabled: bool = False
