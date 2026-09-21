"""Admin API (contract section 3.5) plus the app-visible config route (3.1.2).

Every route here but ``GET /_hull/v1/app-config`` requires the admin token; that one
route requires an app token instead, which is why it lives in its own
:func:`register_app_config` hooked up separately in :func:`register`.
"""
from __future__ import annotations

import re
from datetime import date

from sites.broker.config_store import (
    ConfigStaleError,
    ConfigValidationError,
    cache_seconds_from_env,
)
from sites.broker.http import HttpError, Request, Response, Router, Services
from sites.broker import tokens as broker_tokens

_APP_ENV_ID = re.compile(r"^[0-9a-f]{32}$")


def _app_env_id(params: dict) -> str:
    value = params.get("appEnvId", "")
    if not _APP_ENV_ID.match(value):
        raise HttpError(400, "invalid_app_env_id",
                         "appEnvId must be 32 lowercase hex characters")
    return value


def register(router: Router, services: Services) -> None:
    config = services.config

    def plan(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        body = request.body if isinstance(request.body, dict) else {}
        try:
            result = config.plan(app_env_id, body.get("config") or {})
        except ConfigValidationError as exc:
            raise HttpError(400, "invalid_config", "the compiled config failed validation",
                             errors=exc.errors) from exc
        return Response(200, result)

    def apply(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        body = request.body if isinstance(request.body, dict) else {}
        try:
            result = config.apply(app_env_id, body.get("config") or {},
                                   str(body.get("planDigest") or ""))
        except ConfigValidationError as exc:
            raise HttpError(400, "invalid_config", "the compiled config failed validation",
                             errors=exc.errors) from exc
        except ConfigStaleError as exc:
            raise HttpError(409, "plan_stale",
                             "the plan digest no longer matches the current base") from exc
        return Response(200, result)

    def get_config(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        row = config.get_version(app_env_id)
        if row is None:
            raise HttpError(404, "no_active_config", "this app has no active config")
        return Response(200, row)

    def put_settings(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        body = request.body if isinstance(request.body, dict) else {}
        merged = config.put_settings(app_env_id, body)
        return Response(200, merged)

    def put_secret(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        name = request.params.get("name", "")
        body = request.body if isinstance(request.body, dict) else {}
        value = body.get("value")
        if not name or not isinstance(value, str) or not value:
            raise HttpError(400, "invalid_secret",
                             "a secret needs a name and a non-empty string value")
        config.put_secret(app_env_id, name, value)
        return Response(204)

    def dev_app_token(request: Request) -> Response:
        if not services.dev_tokens_enabled:
            raise HttpError(404, "not_available", "dev tokens are disabled on this deployment")
        app_env_id = _app_env_id(request.params)
        body = request.body if isinstance(request.body, dict) else {}
        site = body.get("site")
        token, expires_at = broker_tokens.issue_app_token(services.keys, app_env_id, site)
        return Response(200, {"token": token, "expiresAt": expires_at})

    def put_user_plan(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        user_id = request.params.get("userId", "")
        body = request.body if isinstance(request.body, dict) else {}
        plan_name = body.get("plan")
        if not isinstance(plan_name, str) or not plan_name:
            raise HttpError(400, "invalid_plan", "plan must be a non-empty string")
        found = config.set_user_plan(app_env_id, user_id, plan_name)
        if not found:
            raise HttpError(404, "unknown_user", "this app has no user with that id")
        return Response(200, {"userId": user_id, "plan": plan_name})

    def usage(request: Request) -> Response:
        app_env_id = _app_env_id(request.params)
        since_raw = request.query.get("since", "")
        try:
            since = date.fromisoformat(since_raw) if since_raw else date.min
        except ValueError as exc:
            raise HttpError(400, "invalid_since", "since must be formatted YYYY-MM-DD") from exc
        try:
            from sites.broker.runtime_api import admin_usage
        except ModuleNotFoundError as exc:
            if exc.name != "sites.broker.runtime_api":
                raise
            raise HttpError(501, "not_available",
                             "usage reporting is not available on this build") from exc
        rows = admin_usage(services, app_env_id, since)
        return Response(200, {"rows": rows})

    def mail(request: Request) -> Response:
        if not services.dev_tokens_enabled:
            raise HttpError(404, "not_available", "mail listing is disabled on this deployment")
        app_env_id = _app_env_id(request.params)
        rows = config.list_mail(app_env_id)
        return Response(200, {"rows": rows})

    def app_config(request: Request) -> Response:
        runtime = request.runtime
        if runtime is None:
            raise HttpError(404, "no_active_config", "this app has no active config")
        compiled = runtime.config
        body = {
            "version": runtime.version,
            "roles": compiled.get("roles"),
            "default_role": compiled.get("default_role"),
            "permissions": compiled.get("permissions"),
            "plans": compiled.get("plans"),
            "default_plan": compiled.get("default_plan"),
            "entities": compiled.get("entities"),
            "auth": compiled.get("auth"),
        }
        max_age = cache_seconds_from_env()
        return Response(200, body, {"Cache-Control": f"max-age={max_age}"})

    router.add("POST", "/_hull/admin/v1/apps/{appEnvId}/config/plan", plan, auth="admin")
    router.add("POST", "/_hull/admin/v1/apps/{appEnvId}/config/apply", apply, auth="admin")
    router.add("GET", "/_hull/admin/v1/apps/{appEnvId}/config", get_config, auth="admin")
    router.add("PUT", "/_hull/admin/v1/apps/{appEnvId}/settings", put_settings, auth="admin")
    router.add("PUT", "/_hull/admin/v1/apps/{appEnvId}/secrets/{name}", put_secret, auth="admin")
    router.add("POST", "/_hull/admin/v1/apps/{appEnvId}/dev-app-token", dev_app_token,
               auth="admin")
    router.add("PUT", "/_hull/admin/v1/apps/{appEnvId}/users/{userId}/plan", put_user_plan,
               auth="admin")
    router.add("GET", "/_hull/admin/v1/apps/{appEnvId}/usage", usage, auth="admin")
    router.add("GET", "/_hull/admin/v1/apps/{appEnvId}/mail", mail, auth="admin")
    router.add("GET", "/_hull/v1/app-config", app_config, auth="app")
