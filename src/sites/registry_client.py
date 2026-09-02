"""Read-only local-registry client with a repository-name whitelist.

Registry responses supply repository names and tags that are later interpolated into URLs.
Unvalidated values would let registry content decide which path the control plane requests,
so the strict whitelist belongs with this client.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from sites.builds import REGISTRY_API, registry_auth_headers


# The registry's repository name/label comes from the registry's own response, and will be spelled into the URL of the next request.
# Not verifying means letting the contents of the registry determine what path we request.
REGISTRY_REPOSITORY_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
REGISTRY_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
REGISTRY_MAX_RESPONSE_BYTES = 1024 * 1024
# The admin console health page should be able to answer questions when they arise, so each detection path has a short timeout and each error is detected.
HEALTH_PROBE_TIMEOUT = 3.0


def registry_get(path: str) -> dict[str, Any]:
    """Fetch one JSON document from the local registry.

    The local registry does not authenticate, so the repository name and label in path must first go through the whitelist by the call point——
    Otherwise a name in the registry response could determine what URL we request next.
    The response size is also capped: it's not our code on the other end of the link.
    """
    request = urllib.request.Request(
        f"{REGISTRY_API}{path}",
        method="GET",
        headers=registry_auth_headers(),
    )
    try:
        with urllib.request.urlopen(
            request, timeout=HEALTH_PROBE_TIMEOUT
        ) as response:
            raw = response.read(REGISTRY_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"local registry returned HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError("local registry is unavailable") from exc
    if len(raw) > REGISTRY_MAX_RESPONSE_BYTES:
        raise RuntimeError("local registry response is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("local registry returned invalid JSON") from exc
    return value if isinstance(value, dict) else {}
