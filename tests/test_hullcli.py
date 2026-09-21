"""`hull` CLI tests: plan/apply/409 flow against a real socket, exit codes, token handling.

A fake Broker (`http.server.ThreadingHTTPServer`) stands in for the real Broker admin API
(contract section 3.5); it only implements the three endpoints `hull` calls
(`config/plan`, `config/apply`, `dev-app-token`) and asserts the admin token it receives,
which is enough to exercise the CLI's request/response handling without partition C's
code. Every case runs the CLI through `hullcli.main`, capturing stdout/stderr the same way
a shell would see them, because the exit code and the stream a message lands on are part
of the contract this module is tested against.
"""
from __future__ import annotations

import contextlib
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sites import hullcli, manifest

ADMIN_TOKEN = "s3cr3t-admin-token"
APP_ENV_ID = "aaaaaaaabbbbbbbbccccccccdddddddd"


def _write_manifest(directory: Path) -> Path:
    (directory / "prompts").mkdir()
    (directory / "prompts" / "triage.md").write_text("You triage tickets.\n", encoding="utf-8")
    (directory / "prompts" / "summarize.md").write_text("Summarize {{input.title}}\n", encoding="utf-8")
    manifest_dict = {
        "app": "support-desk",
        "roles": ["member"],
        "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 200}}},
        "entities": {
            "tickets": {
                "table": "tickets",
                "primary_key": "id",
                "columns": ["id", "title", "status", "owner_id"],
            }
        },
        "permissions": {"member": {"tickets": {"actions": ["read"]}}},
        "secrets": [],
        "capabilities": [
            {
                "alias": "kb",
                "kind": "mcp",
                "url": "https://mcp.example.com/mcp",
                "tools": ["search_docs"],
                "credential": "app",
                "risk": "read",
            }
        ],
        "agents": {
            "triage": {
                "instructions": "prompts/triage.md",
                "model_tier": "balanced",
                "tools": ["entity:tickets.read", "kb.search_docs"],
                "run_as": "end_user",
                "budget": {"max_credits_per_run": 30},
            }
        },
        "flows": {
            "summarize_ticket": {
                "mode": "llm",
                "model_tier": "fast",
                "prompt": "prompts/summarize.md",
                "fallback": {"summary": ""},
                "audience": "server",
                "budget": {"max_credits_per_call": 10},
            }
        },
    }
    path = directory / "hull.json"
    path.write_text(json.dumps(manifest_dict), encoding="utf-8")
    return path


class _FakeBrokerHandler(BaseHTTPRequestHandler):
    """Implements just enough of the admin API (contract 3.5) for the CLI tests below."""

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def _respond(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler naming)
        if self.headers.get("Authorization") != f"Bearer {ADMIN_TOKEN}":
            self._respond(401, {"error": "bad admin token", "code": "invalid_admin_token"})
            return
        body = self._body()
        state = self.server.state  # type: ignore[attr-defined]
        if self.path == f"/_hull/admin/v1/apps/{APP_ENV_ID}/config/plan":
            if body.get("config", {}).get("app") == "reject-me":
                self._respond(400, {"errors": ["/app: the fake broker refuses this app name"]})
                return
            self._respond(
                200,
                {
                    "digest": "digest-" + str(state["plan_calls"]),
                    "activeVersion": state["version"],
                    "changes": [{"path": "/app", "op": "add"}],
                },
            )
            state["plan_calls"] += 1
        elif self.path == f"/_hull/admin/v1/apps/{APP_ENV_ID}/config/apply":
            if state["applied_once"]:
                self._respond(409, {"error": "stale plan", "code": "plan_stale"})
                return
            state["applied_once"] = True
            state["version"] = (state["version"] or 0) + 1
            self._respond(200, {"version": state["version"], "digest": body.get("planDigest")})
        elif self.path == f"/_hull/admin/v1/apps/{APP_ENV_ID}/dev-app-token":
            self._respond(200, {"token": "dev-token-xyz", "expiresAt": "2026-01-01T00:00:00Z"})
        else:
            self._respond(404, {"error": "not_found"})


class HullCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBrokerHandler)
        cls.server.state = {"plan_calls": 0, "applied_once": False, "version": None}
        cls.broker_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.server.state = {"plan_calls": 0, "applied_once": False, "version": None}
        self.tmp = self.enterContext(_temp_dir())
        self.manifest_path = _write_manifest(self.tmp)
        self.token_file = self.tmp / "admin-token"
        self.token_file.write_text(ADMIN_TOKEN, encoding="utf-8")

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hullcli.main(argv)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _err_json(err: str) -> dict:
        """`apply` also writes a human-readable plan preview to stderr before the final
        JSON error line; only that last line is the machine-readable envelope."""
        return json.loads(err.strip().splitlines()[-1])

    # --- validate / compile --------------------------------------------
    def test_validate_a_good_manifest(self) -> None:
        code, out, err = self._run(["validate", str(self.manifest_path)])
        self.assertEqual(0, code, err)
        self.assertEqual({"ok": True}, json.loads(out))

    def test_validate_reports_errors_on_stderr_with_exit_2(self) -> None:
        broken = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        del broken["app"]
        self.manifest_path.write_text(json.dumps(broken), encoding="utf-8")
        code, out, err = self._run(["validate", str(self.manifest_path)])
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertTrue(any(e.startswith("/app:") for e in json.loads(err)["errors"]))

    def test_compile_prints_inlined_manifest(self) -> None:
        code, out, err = self._run(["compile", str(self.manifest_path)])
        self.assertEqual(0, code, err)
        compiled = json.loads(out)
        self.assertEqual("You triage tickets.\n", compiled["agents"]["triage"]["instructions"])
        self.assertEqual([], manifest.validate(compiled))

    def test_default_manifest_path_falls_back_to_hull_json(self) -> None:
        # hull.yaml does not exist in this temp dir, only hull.json does.
        import os

        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            code, out, err = self._run(["validate"])
        finally:
            os.chdir(cwd)
        self.assertEqual(0, code, err)

    def test_missing_manifest_is_a_clear_error(self) -> None:
        import os
        import tempfile

        cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as empty:
                os.chdir(empty)
                code, out, err = self._run(["validate"])
        finally:
            os.chdir(cwd)
        self.assertEqual(2, code)
        self.assertIn("hull.yaml", json.loads(err)["error"])
        self.assertIn("hull.json", json.loads(err)["error"])

    # --- plan ------------------------------------------------------------
    def test_plan_prints_the_broker_response(self) -> None:
        code, out, err = self._run(
            [
                "plan",
                "--broker",
                self.broker_url,
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
                str(self.manifest_path),
            ]
        )
        self.assertEqual(0, code, err)
        result = json.loads(out)
        self.assertIn("digest", result)
        self.assertEqual([{"path": "/app", "op": "add"}], result["changes"])

    def test_plan_reports_a_broker_side_rejection(self) -> None:
        broken = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        broken["app"] = "reject-me"
        self.manifest_path.write_text(json.dumps(broken), encoding="utf-8")
        code, out, err = self._run(
            [
                "plan",
                "--broker",
                self.broker_url,
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
                str(self.manifest_path),
            ]
        )
        self.assertEqual(2, code)
        self.assertIn("the fake broker refuses this app name", json.loads(err)["error"])

    def test_plan_without_admin_token_fails_clearly(self) -> None:
        code, out, err = self._run(
            ["plan", "--broker", self.broker_url, "--app", APP_ENV_ID, str(self.manifest_path)]
        )
        self.assertEqual(1, code)
        self.assertIn("admin token", json.loads(err)["error"])

    # --- apply -------------------------------------------------------------
    def test_apply_with_yes_publishes_and_prints_the_version(self) -> None:
        code, out, err = self._run(
            [
                "apply",
                "--broker",
                self.broker_url,
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
                "--yes",
                str(self.manifest_path),
            ]
        )
        self.assertEqual(0, code, err)
        result = json.loads(out)
        self.assertEqual(1, result["version"])
        self.assertIn("Plan digest", err)

    def test_apply_without_yes_and_no_tty_refuses(self) -> None:
        code, out, err = self._run(
            [
                "apply",
                "--broker",
                self.broker_url,
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
                str(self.manifest_path),
            ]
        )
        self.assertEqual(1, code)
        self.assertIn("--yes", self._err_json(err)["error"])

    def test_apply_after_someone_else_applied_reports_conflict_and_exit_3(self) -> None:
        argv = [
            "apply",
            "--broker",
            self.broker_url,
            "--app",
            APP_ENV_ID,
            "--admin-token-file",
            str(self.token_file),
            "--yes",
            str(self.manifest_path),
        ]
        code, _, _ = self._run(argv)
        self.assertEqual(0, code)
        code, out, err = self._run(argv)
        self.assertEqual(3, code)
        self.assertIn("re-run plan", self._err_json(err)["error"])

    def test_admin_token_value_never_appears_in_any_output(self) -> None:
        code, out, err = self._run(
            [
                "apply",
                "--broker",
                self.broker_url,
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
                "--yes",
                str(self.manifest_path),
            ]
        )
        self.assertNotIn(ADMIN_TOKEN, out)
        self.assertNotIn(ADMIN_TOKEN, err)

    # --- dev-token -----------------------------------------------------
    def test_dev_token(self) -> None:
        code, out, err = self._run(
            [
                "dev-token",
                "--broker",
                self.broker_url,
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
            ]
        )
        self.assertEqual(0, code, err)
        self.assertEqual("dev-token-xyz", json.loads(out)["token"])

    # --- broker unreachable ------------------------------------------------
    def test_unreachable_broker_is_exit_1(self) -> None:
        code, out, err = self._run(
            [
                "plan",
                "--broker",
                "http://127.0.0.1:1",
                "--app",
                APP_ENV_ID,
                "--admin-token-file",
                str(self.token_file),
                str(self.manifest_path),
            ]
        )
        self.assertEqual(1, code)
        self.assertIn("cannot reach the broker", json.loads(err)["error"])


@contextlib.contextmanager
def _temp_dir():
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        yield Path(name)


if __name__ == "__main__":
    unittest.main()
