"""Everything KubeClient can fail with has to arrive as ApiError or RuntimeError.

Every caller guards on that pair -- `activator._is_awake` says "Must not raise"
in its docstring and catches exactly those two -- so a failure that arrives as
anything else passes through the guard, through the handler, and reaches the
socket server, which closes the connection without a response. For the
activator that is the failure its P0 test exists to prevent: a request to a site
that is up and serving gets nothing back because the apiserver had a bad moment.

`urlopen` wraps what it raises itself. A connection reset, an SSL error or a
short read during `read()` arrives bare, and a proxy answering with an HTML
error page fails in `json.loads`. None of those is a RuntimeError.
"""

from __future__ import annotations

import http.client
import ssl
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sites import kube  # noqa: E402


class _Response:
    def __init__(self, error: BaseException | None = None, body: bytes = b"{}"):
        self._error = error
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        if self._error is not None:
            raise self._error
        return self._body


#: Keyed so a type that gains no case fails test_every_declared_failure_has_a_case.
DURING_READ = {
    "connection reset": ConnectionResetError("reset by peer"),
    # An HTTPException, not an OSError. Catching only OSError misses it.
    "short read": http.client.IncompleteRead(b"ab", 100),
    "tls failure": ssl.SSLError("record layer failure"),
    "server hung up": http.client.RemoteDisconnected("closed without response"),
}


class KubeErrorNormalisation(unittest.TestCase):
    def _client(self) -> kube.KubeClient:
        # http:// so the constructor builds no SSL context and needs no CA file
        # on disk. What is under test is the request path, not TLS setup.
        return kube.KubeClient(base_url="http://apiserver.invalid", token="t")

    def _failure(self, response: _Response) -> BaseException:
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(Exception) as raised:
                self._client().request("GET", "/api/v1/namespaces")
        return raised.exception

    def test_a_body_read_failure_is_normalised(self) -> None:
        for name, error in DURING_READ.items():
            with self.subTest(failure=name):
                self.assertIsInstance(self._failure(_Response(error=error)), RuntimeError)

    def test_a_non_json_body_is_normalised(self) -> None:
        # An ingress or proxy in front of the apiserver answering with HTML.
        failure = self._failure(_Response(body=b"<html>502 Bad Gateway</html>"))
        self.assertIsInstance(failure, RuntimeError)

    def test_a_good_response_still_parses(self) -> None:
        # Without this the whole file passes against a request() that raises
        # RuntimeError unconditionally.
        with mock.patch("urllib.request.urlopen", return_value=_Response(body=b'{"ok": true}')):
            self.assertEqual({"ok": True}, self._client().request("GET", "/api/v1/x"))

    def test_every_declared_failure_has_a_case(self) -> None:
        # The tuple in kube.py and this table have to stay in step; a branch
        # nobody feeds is indistinguishable from one that cannot match.
        self.assertEqual(4, len(DURING_READ))
        self.assertTrue(
            any(isinstance(e, http.client.HTTPException) for e in DURING_READ.values()),
            "no HTTPException case: OSError alone would look sufficient",
        )
        self.assertTrue(
            any(isinstance(e, OSError) and not isinstance(e, http.client.HTTPException)
                for e in DURING_READ.values()),
            "no plain OSError case",
        )


if __name__ == "__main__":
    unittest.main()
