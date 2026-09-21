"""Vertical-slice acceptance run across the real agent runtime and the real Broker.

Processes: a scripted OpenAI-compatible model (fake_llm.py), a one-tool MCP server
(fake_mcp.py), the agent runtime from a hullwork/agent checkout that has the
caller-tools extension (subprocess, its own venv), and the Broker gateway (in this
process). One thing is bent for the run: the MCP client's public-address check is
relaxed to admit the loopback fake MCP server; that guard has its own unit tests.

Run from this repository with its venv:
  HULL_E2E_AGENT_DIR=/path/to/agent-checkout \\
  HULL_E2E_PG="host=127.0.0.1 port=5432 user=postgres password=... sslmode=disable" \\
  .venv/bin/python examples/ai-saas-slice/e2e/run_e2e.py
The agent checkout needs `uvx uv@0.12.3 sync --locked --all-extras` first, the
PostgreSQL user must be able to create databases and roles, and this repository's
`sdk/ts` is built on first use (needs node >= 20 and npm).
Exit code 0 means every acceptance check passed.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.request
from pathlib import Path

import psycopg

SITE = Path(__file__).resolve().parents[3]
AGENT = Path(os.environ["HULL_E2E_AGENT_DIR"]).resolve()
E2E = Path(__file__).resolve().parent
PG = os.environ["HULL_E2E_PG"]
BROKER_DB, APP_DB = "hull_e2e_broker", "hull_e2e_app"
APP_ROLE, APP_ROLE_PASSWORD = "hull_e2e_app_rt", "e2e-app-rt-password"
BROKER_DSN = f"{PG} dbname={BROKER_DB}"
APP_ADMIN_DSN = f"{PG} dbname={APP_DB}"
_APP_BASE = " ".join(p for p in PG.split() if not p.startswith(("user=", "password=")))
APP_RUNTIME_DSN = f"{_APP_BASE} dbname={APP_DB} user={APP_ROLE} password={APP_ROLE_PASSWORD}"
PROXY_SECRET = "e2e-proxy-secret-" + "x" * 24
KB_TOKEN = "kb-secret-token-e2e"
ADMIN_TOKEN = "admin-token-e2e-" + "y" * 24
APP1 = "a" * 32
APP2 = "b" * 32

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


def request(port: int, method: str, path: str, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw.decode("utf-8", "replace")
    return resp.status, dict(resp.getheaders()), parsed


def sse(port: int, path: str, body, headers) -> tuple[int, list[tuple[str, dict]]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("POST", path, body=json.dumps(body).encode(),
                 headers={"Content-Type": "application/json", **headers})
    resp = conn.getresponse()
    if resp.status != 200:
        raw = resp.read()
        conn.close()
        return resp.status, [("http", json.loads(raw) if raw else {})]
    events, name, data = [], None, []
    for line in resp:
        line = line.decode().rstrip("\n")
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "" and name:
            events.append((name, json.loads("\n".join(data)) if data else {}))
            name, data = None, []
    conn.close()
    return 200, events


def sql(dsn: str, statement: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement)


def recreate_database(name: str) -> None:
    sql(f"{PG} dbname=postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    sql(f"{PG} dbname=postgres", f'CREATE DATABASE "{name}"')


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="hw-e2e-"))
    procs: list[subprocess.Popen] = []
    try:
        # ---- fakes -------------------------------------------------------------
        llm_log, mcp_log = work / "llm.jsonl", work / "mcp.jsonl"
        llm = subprocess.Popen([sys.executable, str(E2E / "fake_llm.py")], stdout=subprocess.PIPE, text=True,
                               env={**os.environ, "FAKE_LLM_LOG": str(llm_log),
                                    "FAKE_LLM_TOOL_ORDER": "entity__tickets__read,kb__search_docs"})
        procs.append(llm)
        llm_port = int(llm.stdout.readline())
        mcp = subprocess.Popen([sys.executable, str(E2E / "fake_mcp.py")], stdout=subprocess.PIPE, text=True,
                               env={**os.environ, "FAKE_MCP_LOG": str(mcp_log), "FAKE_MCP_TOKEN": KB_TOKEN})
        procs.append(mcp)
        mcp_port = int(mcp.stdout.readline())

        # ---- agent runtime (caller-tools branch) --------------------------------
        agent_port = free_port()
        agent_env = {**os.environ,
                     "AGENT_EPHEMERAL_PREVIEW": "1", "AGENT_HOST": "127.0.0.1", "AGENT_PORT": str(agent_port),
                     "AGENT_TRUST_PROXY_AUTH": "1", "AGENT_PROXY_SECRET": PROXY_SECRET,
                     "AGENT_CALLER_TOOLS": "1", "AGENT_MODEL": "fake-model",
                     "OPENAI_API_KEY": "fake", "OPENAI_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
                     "AGENT_ACTING_SUBJECT_SALT": "s" * 40}
        agent_log = open(work / "agent.log", "w")
        agent = subprocess.Popen([str(AGENT / ".venv/bin/python"), "-c",
                                  "from server.runtime import main; main()"],
                                 cwd=AGENT, env=agent_env, stdout=agent_log, stderr=subprocess.STDOUT)
        procs.append(agent)
        wait_http(f"http://127.0.0.1:{agent_port}/readyz")

        # ---- app database: tickets with FORCEd RLS, a non-superuser runtime role -
        recreate_database(APP_DB)
        sql(f"{PG} dbname=postgres",
            f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{APP_ROLE}') THEN "
            f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER PASSWORD '{APP_ROLE_PASSWORD}'; END IF; END$$;")
        sql(APP_ADMIN_DSN, (SITE / "examples/ai-saas-slice/migrations/001_tickets.sql").read_text())
        sql(APP_ADMIN_DSN, f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}; GRANT SELECT ON tickets TO {APP_ROLE};")

        # ---- Broker gateway, in process -----------------------------------------
        recreate_database(BROKER_DB)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        (work / "keys.json").write_text(json.dumps([{"kid": "k1", "private_key_pem": pem, "active": True}]))
        (work / "secret.key").write_bytes(os.urandom(32))
        (work / "admin.token").write_text(ADMIN_TOKEN)
        (work / "proxy.secret").write_text(PROXY_SECRET)
        os.environ.update({
            "HULL_BROKER_DB_DSN": BROKER_DSN,
            "HULL_BROKER_SIGNING_KEYS_FILE": str(work / "keys.json"),
            "HULL_BROKER_SECRET_KEY_FILE": str(work / "secret.key"),
            "HULL_BROKER_ADMIN_TOKEN_FILE": str(work / "admin.token"),
            "HULL_BROKER_DEV_TOKENS": "1",
            "HULL_CONFIG_CACHE_SECONDS": "0",
            "HULL_LLM_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
            "HULL_AGENT_RUNTIME_URL": f"http://127.0.0.1:{agent_port}",
            "HULL_AGENT_PROXY_SECRET_FILE": str(work / "proxy.secret"),
        })
        sys.path.insert(0, str(SITE / "src"))
        from sites.broker import __main__ as broker_main, mcp_client, server as broker_server
        mcp_client._test_state = types.SimpleNamespace(enabled=True)  # admit the loopback fake MCP only
        services = broker_main._build_services()
        router = broker_server.build_router(services)
        httpd = broker_server.listen("127.0.0.1", 0, router, services)
        broker_port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        broker_url = f"http://127.0.0.1:{broker_port}"
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

        # ---- manifest through the hull CLI (plan + apply) ------------------------
        app_dir = work / "app"
        shutil.copytree(SITE / "examples/ai-saas-slice", app_dir,
                        ignore=shutil.ignore_patterns("node_modules"))
        manifest = (app_dir / "hull.yaml").read_text().replace(
            "https://mcp.example.test/kb", f"http://127.0.0.1:{mcp_port}/mcp")
        (app_dir / "hull.yaml").write_text(manifest)
        cli_env = {**os.environ, "HULL_BROKER_ADMIN_TOKEN_FILE": str(work / "admin.token")}
        py = str(SITE / ".venv/bin/python")
        for app in (APP1, APP2):
            out = subprocess.run([py, "-m", "sites.hullcli", "apply", "--broker", broker_url, "--app", app,
                                  "--yes", str(app_dir / "hull.yaml")],
                                 env=cli_env, capture_output=True, text=True)
            check(f"hull apply succeeds for app {app[:4]}", out.returncode == 0,
                  (out.stdout + out.stderr).strip()[-300:])
            leaked = ADMIN_TOKEN in (out.stdout + out.stderr)
            check(f"hull apply output never contains the admin token ({app[:4]})", not leaked)

        tiers = {"fast": {"model": "fake-model", "input_credits_per_1k": 1, "output_credits_per_1k": 2},
                 "balanced": {"model": "fake-model", "input_credits_per_1k": 1, "output_credits_per_1k": 2}}
        for app, host in ((APP1, "slice.test"), (APP2, "slice2.test")):
            st, _, body = request(broker_port, "PUT", f"/_hull/admin/v1/apps/{app}/settings", {
                "hosts": [host], "ai_enabled": True, "model_tiers": tiers,
                "flows": {"summarize_ticket": {"enabled": True}},
                "database_secret": "database_dsn", "capability_secrets": {"kb": "kb_token"},
                "env": "dev"}, admin)
            check(f"settings accepted for {host}", st == 200, str(body)[:200])
            for name, value in (("database_dsn", APP_RUNTIME_DSN), ("kb_token", KB_TOKEN)):
                st, _, _ = request(broker_port, "PUT", f"/_hull/admin/v1/apps/{app}/secrets/{name}",
                                   {"value": value}, admin)
                check(f"secret {name} stored for {host}", st == 204)
            st, _, body = request(broker_port, "GET", f"/_hull/admin/v1/apps/{app}/config", None, admin)
            check(f"no secret value appears in the config response ({host})",
                  st == 200 and KB_TOKEN not in json.dumps(body) and APP_ROLE_PASSWORD not in json.dumps(body))

        # ---- 1. sign-in on the app's own domain ----------------------------------
        def sign_in(host: str, app: str, email: str):
            st, _, body = request(broker_port, "POST", "/_hull/auth/email/start", {"email": email},
                                  {"Host": host})
            if st != 202:
                return None, None, f"start {st} {body}"
            st, _, mail = request(broker_port, "GET", f"/_hull/admin/v1/apps/{app}/mail", None, admin)
            rows = [r for r in (mail or {}).get("rows", []) if r.get("to") == email]  # newest first
            codes = re.findall(r"\b(\d{6})\b", rows[0]["body"]) if rows else []
            if not codes:
                return None, None, "no code mailed"
            st, headers, body = request(broker_port, "POST", "/_hull/auth/email/verify",
                                        {"email": email, "code": codes[0]}, {"Host": host})
            if st != 200:
                return None, None, f"verify {st} {body}"
            return body, headers.get("Set-Cookie", ""), ""

        alice, alice_cookie, err = sign_in("slice.test", APP1, "alice@example.test")
        check("1 sign-in on the app domain returns a user token", alice is not None, err)
        check("1 session cookie is __Host-, HttpOnly, Secure, SameSite=Lax",
              all(s in (alice_cookie or "") for s in ("__Host-hull_session=", "HttpOnly", "Secure", "SameSite=Lax")),
              alice_cookie or "")
        bob, _, err = sign_in("slice.test", APP1, "bob@example.test")
        check("1 second user signs in", bob is not None, err)
        carol, _, err = sign_in("slice2.test", APP2, "carol@example.test")
        check("1 user of another app signs in", carol is not None, err)
        st, _, body = request(broker_port, "POST", "/_hull/auth/email/start", {"email": "x@example.test"},
                              {"Host": "unknown.test"})
        check("1 unknown host is refused", st == 404, str(body))
        st_known, _, b_known = request(broker_port, "POST", "/_hull/auth/email/start",
                                       {"email": "alice@example.test"}, {"Host": "slice.test"})
        st_new, _, b_new = request(broker_port, "POST", "/_hull/auth/email/start",
                                   {"email": "nobody-yet@example.test"}, {"Host": "slice.test"})
        check("1 start does not reveal whether an account exists", (st_known, b_known) == (st_new, b_new))

        # SDK side: verify the user token locally against the live JWKS with the TS SDK.
        sdk = SITE / "sdk/ts"
        if not (sdk / "dist/src/index.js").exists():
            subprocess.run(["npm", "install", "--silent"], cwd=sdk, check=True)
            subprocess.run(["npm", "run", "build", "--silent"], cwd=sdk, check=True)
        js = ("import {createHull} from '%s';"
              "const h=createHull({brokerUrl:'%s',appEnvId:'%s',tokenFile:'/nonexistent'});"
              "try{const u=await h.auth.verifyUserToken(process.argv[1]);console.log(JSON.stringify({ok:true,u}))}"
              "catch(e){console.log(JSON.stringify({ok:false,e:String(e)}))}"
              ) % ((sdk / "dist/src/index.js").as_uri(), broker_url, APP1)
        def sdk_verify(token: str, app: str = APP1) -> dict:
            code = js.replace(APP1, app)
            out = subprocess.run(["node", "--input-type=module", "-e", code, token],
                                 capture_output=True, text=True)
            try:
                return json.loads(out.stdout.strip().splitlines()[-1])
            except Exception:
                return {"ok": False, "e": out.stdout + out.stderr}
        if alice:
            v = sdk_verify(alice["token"])
            check("1 TS SDK verifies the user token against the live JWKS", v.get("ok"), str(v)[:200])
        if carol:
            v = sdk_verify(carol["token"])
            check("1 TS SDK refuses a token issued for another app", not v.get("ok"), str(v)[:200])

        # ---- app identity ----------------------------------------------------------
        st, _, tok = request(broker_port, "POST", f"/_hull/admin/v1/apps/{APP1}/dev-app-token", {}, admin)
        app1_token = tok["token"] if st == 200 else ""
        st, _, tok = request(broker_port, "POST", f"/_hull/admin/v1/apps/{APP2}/dev-app-token", {}, admin)
        app2_token = tok["token"] if st == 200 else ""
        check("dev app tokens issued", bool(app1_token and app2_token))

        def as_user(user, app_token=app1_token, extra=None):
            h = {"Authorization": f"Bearer {app_token}", "X-Hull-User": user["token"]}
            h.update(extra or {})
            return h

        # tickets owned by each user
        a_id, b_id = alice["user"]["id"], bob["user"]["id"]
        sql(APP_ADMIN_DSN, "INSERT INTO tickets (title, body, owner_id) VALUES "
                       f"('ALICE-TICKET-1','printer broken','{a_id}'),"
                       f"('ALICE-TICKET-2','cannot log in','{a_id}'),"
                       f"('BOB-TICKET-1','refund please','{b_id}');")

        # ---- 2. a flow through the Broker ------------------------------------------
        st, _, body = request(broker_port, "POST", "/_hull/v1/flows/summarize_ticket/run",
                              {"input": {"title": "t", "body": "the printer is broken"}},
                              as_user(alice, extra={"Idempotency-Key": "flow-1"}))
        check("2 flow returns a structured result", st == 200 and body.get("fallback") is False
              and isinstance(body.get("output"), dict) and "summary" in body["output"], str(body)[:300])
        st2, _, body2 = request(broker_port, "POST", "/_hull/v1/flows/summarize_ticket/run",
                                {"input": {"title": "t", "body": "the printer is broken"}},
                                as_user(alice, extra={"Idempotency-Key": "flow-1"}))
        check("2 same idempotency key replays without a second charge", body2 == body, str(body2)[:200])
        st, _, body = request(broker_port, "POST", "/_hull/v1/flows/summarize_ticket/run",
                              {"input": {"title": 3}}, as_user(alice))
        check("2 input violating the schema is refused", st == 400, str(body)[:200])

        # ---- 3. an agent as the end user, entity read + external MCP tool ----------
        def agent_run(user) -> tuple[int, list]:
            return sse(broker_port, "/_hull/v1/agents/triage/runs", {"input": "triage my tickets"},
                       as_user(user))

        before = llm_log.read_text().count("\n") if llm_log.exists() else 0
        st, events = agent_run(alice)
        names = [e[0] for e in events]
        check("3 agent run streams to run.finished", st == 200 and names and names[-1] == "run.finished",
              f"{len(names)} events, last: {names[-3:]} {events[-1][1] if events else ''}")
        tools_used = [e[1].get("tool") for e in events if e[0] == "tool.finished" and e[1].get("ok")]
        check("3 both tools ran through the Broker gate", len(tools_used) >= 2, str(events)[:400])
        new_lines = llm_log.read_text().splitlines()[before:]
        tool_texts = []
        for line in new_lines:
            req = json.loads(line)["request"]
            for m in req.get("messages", []):
                if m.get("role") == "tool":
                    tool_texts.append(str(m.get("content")))
        joined = " ".join(tool_texts)
        check("3 alice's agent saw her tickets", "ALICE-TICKET-1" in joined and "ALICE-TICKET-2" in joined,
              joined[:300])
        check("3 alice's agent did not see bob's ticket", "BOB-TICKET-1" not in joined)
        check("3 tool results reached the model marked as untrusted data",
              "untrusted" in joined.lower(), joined[:200])
        mcp_calls = [json.loads(l) for l in mcp_log.read_text().splitlines()] if mcp_log.exists() else []
        check("3 the MCP server received the app credential",
              any(c["message"].get("method") == "tools/call" and c["auth_ok"] for c in mcp_calls))
        offered = []
        for line in new_lines:
            offered += [t.get("function", {}).get("name") for t in json.loads(line)["request"].get("tools") or []]
        check("3 the model was offered no sandbox or deployment tools",
              not any(n and (n in ("bash", "shell", "write_file", "read_file") or n.startswith("mcp_site"))
                      for n in offered), str(sorted(set(filter(None, offered))))[:300])

        before = llm_log.read_text().count("\n")
        st, events = agent_run(bob)
        joined = " ".join(str(m.get("content")) for line in llm_log.read_text().splitlines()[before:]
                          for m in json.loads(line)["request"].get("messages", []) if m.get("role") == "tool")
        check("3 bob's agent saw only bob's ticket", "BOB-TICKET-1" in joined and "ALICE" not in joined,
              joined[:300])

        # ---- 5. usage visible per user ------------------------------------------------
        st, _, body = request(broker_port, "GET", "/_hull/v1/usage/me", None, as_user(alice))
        check("5 user sees own usage", st == 200 and body.get("used", 0) > 0, str(body))
        st, _, body = request(broker_port, "GET", f"/_hull/admin/v1/apps/{APP1}/usage?since=2000-01-01",
                              None, admin)
        users = {r.get("userId") for r in (body or {}).get("rows", [])}
        check("5 admin usage is broken down by user", st == 200 and a_id in users and b_id in users, str(body)[:300])

        # ---- 4. budget exhaustion ------------------------------------------------------
        low = manifest.replace("ai_credits_month: 200", "ai_credits_month: 1")
        (app_dir / "hull.yaml").write_text(low)
        out = subprocess.run([py, "-m", "sites.hullcli", "apply", "--broker", broker_url, "--app", APP1,
                              "--yes", str(app_dir / "hull.yaml")], env=cli_env, capture_output=True, text=True)
        check("4 lowered plan limit applied as a new version", out.returncode == 0,
              (out.stdout + out.stderr)[-200:])
        # the user token carries the plan name, not the limit; the limit comes from config
        st, _, body = request(broker_port, "POST", "/_hull/v1/flows/summarize_ticket/run",
                              {"input": {"title": "t", "body": "b"}}, as_user(alice))
        check("4 flow falls back when the user's credits are exhausted",
              st == 200 and body.get("fallback") is True and body.get("reason") == "budget_exhausted",
              str(body)[:200])
        st, events = agent_run(alice)
        check("4 agent run is refused when the user's credits are exhausted",
              st == 402 or any(e[0] == "run.error" for e in events), str(events)[:200])

        # ---- 6. isolation --------------------------------------------------------------
        st, _, body = request(broker_port, "POST", "/_hull/v1/flows/summarize_ticket/run",
                              {"input": {"title": "t", "body": "b"}},
                              {"Authorization": f"Bearer {app1_token}", "X-Hull-User": carol["token"]})
        check("6 app A token with a user of app B is refused", st == 401, str(body)[:200])
        st, _, body = request(broker_port, "GET", f"/_hull/admin/v1/apps/{APP2}/config", None,
                              {"Authorization": f"Bearer {app1_token}"})
        check("6 an app token cannot use the admin API", st in (401, 403), str(body)[:200])
        st, _, body = request(broker_port, "POST", "/_hull/v1/flows/summarize_ticket/run",
                              {"input": {"title": "t", "body": "b"}}, {"X-Hull-User": alice["token"]})
        check("6 no app token is refused", st == 401, str(body)[:200])
        st, events = sse(broker_port, "/_hull/v1/agents/triage/runs", {"input": "hi"},
                         {"Authorization": f"Bearer {app2_token}", "X-Hull-User": carol["token"]})
        joined = json.dumps(events)
        check("6 an agent of app B never sees app A's rows", "ALICE" not in joined and "BOB" not in joined)

        httpd.shutdown()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        print(f"work dir: {work}")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
