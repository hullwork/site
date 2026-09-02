"""Minimal Kubernetes API client based on urllib.

The client supports the five verbs Sites needs and deliberately has no watch support. Both
the operator and synchronization paths use polling, which keeps transport dependencies and
reconnection behavior bounded.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from pathlib import Path
from typing import Any

from sites import tracing


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Kubernetes API {status}: {message}")
        self.status = status
        self.message = message


class KubeClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        ca_file: str | None = None,
        timeout: float = 10.0,
    ):
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.base_url = (base_url or f"https://{host}:{port}").rstrip("/")
        token_path = Path(
            "/var/run/secrets/kubernetes.io/serviceaccount/token"
        )
        self.token = token if token is not None else token_path.read_text().strip()
        default_ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        selected_ca = ca_file or default_ca
        self.context = (
            ssl.create_default_context(cafile=selected_ca)
            if self.base_url.startswith("https://")
            else None
        )
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        content_type: str = "application/json",
    ) -> dict[str, Any] | None:
        with tracing.span(
            "kubernetes.api.request",
            kind=3,
            attributes={"http.request.method": method, "server.address": self.base_url},
        ) as request_span:
            try:
                return self._request(method, path, body, content_type=content_type)
            except BaseException as exc:
                request_span.set_error(exc)
                raise

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        content_type: str = "application/json",
    ) -> dict[str, Any] | None:
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": content_type,
                **tracing.outbound_headers(),
            },
        )
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=self.timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("message", raw)
            except json.JSONDecodeError:
                detail = raw
            raise ApiError(exc.code, str(detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Kubernetes API unavailable: {exc.reason}") from exc
        except TimeoutError as exc:
            # A timeout while reading the response body escapes urlopen bare
            # rather than wrapped in URLError, so it needs its own branch.
            raise RuntimeError(
                f"Kubernetes API timed out after {self.timeout}s"
            ) from exc

        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def get(self, path: str) -> dict[str, Any]:
        result = self.request("GET", path)
        return result or {}

    def create(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", path, body)
        return result or {}

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request(
            "PATCH",
            path,
            body,
            content_type="application/merge-patch+json",
        )
        return result or {}

    def delete(
        self, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Delete a resource, optionally with an explicit DeleteOptions body.

        Without a body the API server applies each resource's own default
        garbage collection policy, and batch/v1 Jobs default to orphaning their
        dependents — the reason ``kubectl delete job`` has to pass
        ``--cascade=background`` explicitly.
        """
        result = self.request("DELETE", path, body)
        return result or {}

    def create_or_patch(
        self,
        collection_path: str,
        resource_path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            self.get(resource_path)
        except ApiError as exc:
            if exc.status != 404:
                raise
            try:
                return self.create(collection_path, body)
            except ApiError as create_exc:
                if create_exc.status != 409:
                    raise
        return self.patch(resource_path, body)
