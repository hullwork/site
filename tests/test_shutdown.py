"""SIGTERM handling shared by the api / operator / activator entry points."""
from __future__ import annotations

import os
import signal
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sites import shutdown


class _Quiet(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


class StopHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }

    def tearDown(self) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)

    def test_sigterm_sets_event_and_runs_callbacks_once(self) -> None:
        calls: list[str] = []
        done = threading.Event()
        stop = shutdown.install_stop_handler(
            lambda: calls.append("first"),
            lambda: (calls.append("second"), done.set()),
        )
        self.assertFalse(stop.is_set())
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(stop.wait(2.0), "stop event must be set by the handler")
        self.assertTrue(done.wait(2.0), "callbacks must run on the shutdown thread")
        # A second signal must not replay the callbacks.
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.1)
        self.assertEqual(calls, ["first", "second"])

    def test_failing_callback_does_not_block_the_rest(self) -> None:
        done = threading.Event()

        def boom() -> None:
            raise RuntimeError("shutdown hook failed")

        shutdown.install_stop_handler(boom, done.set)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(done.wait(2.0))

    def test_sigterm_stops_a_serving_http_server(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
        returned = threading.Event()

        def serve() -> None:
            server.serve_forever(poll_interval=0.05)
            returned.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        shutdown.install_stop_handler(server.shutdown)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(
            returned.wait(3.0), "serve_forever must return after SIGTERM"
        )
        server.server_close()


if __name__ == "__main__":
    unittest.main()
