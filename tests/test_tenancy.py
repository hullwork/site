"""End-to-end regression for multi-tenant isolation.

Runs against a real control plane with real HTTP and PostgreSQL, faking only Kubernetes.
Authorization defects often hide between caller layers, so stubbing authentication or
storage would conceal the boundary under test.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import threading
import time
import types
import unittest
from typing import Any
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from sites.api import Handler
from sites.exposure import NODE_PORT_RANGE
from sites import exposure as _exposure
from sites.k8s_resources import site_deployment_resource
from sites.naming import (
    cr_name_for,
    namespace_for_tenant,
    new_merchant_api_key,
    new_tenant_token,
    token_digest,
)
from sites.validation import DEFAULT_MERCHANT_ID
from sites.storage import (
    _POSTGRES,
    _SCHEMA_STEPS,
    StorageConflictError,
    StorageError,
    Store,
    _register_schema_step,
)
from tests.test_support import postgres_connection, postgres_store


ADMIN_TOKEN = "a" * 32
SESSION_KEY = "k" * 32


KATE = "1" * 32
SHARED = "2" * 32


def subject_for(name: str) -> str:
    """A well-formed acting-subject pseudonym for a readable test name.

    The wire shape is fixed (32 lowercase hex) and this is not the derivation under test -
    sites.client.acting_subject is, in test_interface. Here it only has to be a legal value
    that differs per name.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


# Port pools are a concept exclusive to the NodePort backend. There is no pool under the Gateway backend (Host is represented by serviceName
# derived), the following use cases are therefore semantically inapplicable - they will end up with KeyError: 'nodePort' or
# The form of "Quota 9 > Pool 8 was accepted" failed, and those two things are exactly the desired results of the transformation.
# Explicitly skip instead of making them red: after cutting the backend CI all red will be read as "I changed it".
_ALLOCATES_PORTS = _exposure.backend().allocates_ports
_POOL_ONLY = unittest.skipUnless(
    _ALLOCATES_PORTS, "The port pool use case only applies to the NodePort backend"
)


class _FakeKube:
    """The SiteDeployment collection in memory is sufficient for access and conflict checking on the API side."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.lock = threading.Lock()

    def get(self, path: str) -> dict:
        with self.lock:
            if path.endswith("/sitebuilds"):
                # The admission check will count in-progress construction into the occupation of the public route. These use cases do not involve source code
                # Build, the collection is empty - but it must be answered, not 404.
                return {"items": []}
            if path.endswith("/sitedeployments"):
                return {"items": [json.loads(json.dumps(o))
                                  for o in self.objects.values()]}
            name = path.rsplit("/", 1)[-1]
            # When calling GET, you also need to recognize collection. self.objects is a flat dictionary, while
            # SiteDeployment has exactly the same name derivation as SiteBuild (both cr_name_for),
            # Without looking at the path, an existing deployment will be read as a build with the same name.
            # ——The performance is that redeploying the same service will result in 409 service_name_conflict.
            # This substitute does not simulate SiteBuild (the above collection path has been declared to return empty items), then it
            # The named GET should always be 404, which is consistent with the fact that the two CRDs in the real cluster are invisible to each other.
            if "/sitebuilds/" not in path and name in self.objects:
                return json.loads(json.dumps(self.objects[name]))
        from sites.kube import ApiError

        raise ApiError(404, "not found")

    def create_or_patch(self, _collection: str, path: str, body: dict) -> dict:
        with self.lock:
            name = path.rsplit("/", 1)[-1]
            self.objects[name] = json.loads(json.dumps(body))
            self.objects[name].setdefault("metadata", {})["name"] = name
            return json.loads(json.dumps(self.objects[name]))

    def patch(self, path: str, body: dict) -> dict:
        with self.lock:
            name = path.replace("/status", "").rsplit("/", 1)[-1]
            target = self.objects.setdefault(name, {"metadata": {"name": name}})
            for key, value in body.items():
                if isinstance(value, dict):
                    target.setdefault(key, {}).update(value)
                else:
                    target[key] = value
            return json.loads(json.dumps(target))

    def delete(self, path: str) -> dict:
        with self.lock:
            self.objects.pop(path.rsplit("/", 1)[-1], None)
            return {}


class TenancyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        store = postgres_store(Path(cls._tmp.name) / "tenancy.db")
        store.migrate()
        Handler.kube = _FakeKube()
        Handler.store = store
        Handler.service_token = ADMIN_TOKEN
        Handler.session_key = SESSION_KEY
        Handler.local_login_enabled = True
        Handler.oidc_config = None
        Handler.mutation_lock = threading.Lock()
        Handler.synchronizer = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        Handler.kube.objects.clear()

    # --- helpers -------------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict | None = None,
        subject: str = "",
        token_header: str = "X-Sites-Service-Token",
        declared_user: str = "",
        declared_merchant: str = "",
    ) -> tuple[int, dict]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {token_header: token}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if subject:
            headers["X-Acting-Subject"] = subject
        # Only the negative cases set these two, and they must reach the server verbatim:
        # the assertion is that the control plane refuses them, so a helper that quietly
        # dropped them would test nothing.
        if declared_user:
            headers["X-User-ID"] = declared_user
        if declared_merchant:
            headers["X-Merchant-ID"] = declared_merchant
        request = urlrequest.Request(
            f"{self.url}{path}", data=body, method=method, headers=headers
        )
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return int(response.status), json.loads(response.read() or b"{}")
        except urlerror.HTTPError as exc:
            return int(exc.code), json.loads(exc.read() or b"{}")

    def new_merchant(self, merchant_id: str, *, may_act: bool = True) -> str:
        """Create a merchant and return its API key in plain text - it can only be obtained at the moment of account creation."""
        status, body = self.call(
            "POST",
            "/v1/merchants",
            ADMIN_TOKEN,
            {
                "merchantId": merchant_id,
                "displayName": merchant_id,
                "mayActAsSubjects": may_act,
            },
        )
        self.assertEqual(status, 201, body)
        return body["apiKey"]

    def new_tenant(self, name: str, **quota) -> str:
        status, body = self.call(
            "POST",
            "/v1/tenants",
            ADMIN_TOKEN,
            {"merchantId": DEFAULT_MERCHANT_ID, "userId": name, **quota},
        )
        self.assertEqual(status, 201, body)
        return body["token"]

    def test_create_tenant_db_failure_maps_503_not_409(self):
        # Database failure was disguised as 409 tenant_exists(storage side distortion translation after repair,
        # API triage by exception type) - Troubleshooters should not be led to look for a conflict that does not exist.
        with mock.patch.object(
            Store,
            "create_tenant",
            side_effect=StorageError("tenant creation failed"),
        ):
            status, body = self.call(
                "POST",
                "/v1/tenants",
                ADMIN_TOKEN,
                {"merchantId": DEFAULT_MERCHANT_ID, "userId": "db-down"},
            )
        self.assertEqual(status, 503, body)
        self.assertEqual(body["error"], "database unavailable")

    def test_create_tenant_conflict_remains_409(self):
        with mock.patch.object(
            Store,
            "create_tenant",
            side_effect=StorageConflictError(
                "tenant 'db-down' already exists under merchant 'm'"
            ),
        ):
            status, body = self.call(
                "POST",
                "/v1/tenants",
                ADMIN_TOKEN,
                {"merchantId": DEFAULT_MERCHANT_ID, "userId": "db-down"},
            )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "tenant_exists")

    def test_create_merchant_db_failure_maps_503_not_409(self):
        with mock.patch.object(
            Store,
            "create_merchant",
            side_effect=StorageError("merchant creation failed"),
        ):
            status, body = self.call(
                "POST",
                "/v1/merchants",
                ADMIN_TOKEN,
                {"merchantId": "db-down", "displayName": "db-down"},
            )
        self.assertEqual(status, 503, body)
        self.assertEqual(body["error"], "database unavailable")

    def test_create_merchant_conflict_remains_409(self):
        with mock.patch.object(
            Store,
            "create_merchant",
            side_effect=StorageConflictError(
                "merchant 'db-down' already exists"
            ),
        ):
            status, body = self.call(
                "POST",
                "/v1/merchants",
                ADMIN_TOKEN,
                {"merchantId": "db-down", "displayName": "db-down"},
            )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "merchant_exists")

    @staticmethod
    def scoped(path: str) -> str:
        """Supplement the merchant dimension to the tenant endpoint targeting a single row by {id}.

        user_id is only unique within the merchant, so these endpoints must explicitly carry merchantId - the server is
        If missing, return 400 instead of guessing a default merchant.
        """
        return f"{path}?merchantId={DEFAULT_MERCHANT_ID}"

    def deploy(self, token: str, name: str, **extra) -> tuple[int, dict]:
        return self.call(
            "POST",
            "/v1/deployments",
            token,
            {
                "name": name,
                "image": "example.invalid/app:v1",
                "port": 8080,
                "healthPath": "/",
                **extra,
            },
        )

    # --- tenant lifecycle ----------------------------------------------
    def test_removed_legacy_service_header_is_not_accepted(self) -> None:
        status, body = self.call(
            "GET",
            "/v1/capabilities",
            ADMIN_TOKEN,
            token_header="X-AppForge-Service-Token",
        )
        self.assertEqual(status, 401, body)

    def test_only_admin_may_manage_tenants(self) -> None:
        token = self.new_tenant("lifecycle-a")
        for method, path, payload in (
            ("POST", "/v1/tenants", {"userId": "sneaky"}),
            ("GET", "/v1/tenants", None),
            ("DELETE", self.scoped("/v1/tenants/lifecycle-a"), None),
        ):
            status, _ = self.call(method, path, token, payload)
            self.assertEqual(status, 403, f"{method} {path}")

    def test_the_token_is_returned_once_and_never_listed(self) -> None:
        self.new_tenant("once-a")
        status, body = self.call("GET", "/v1/tenants", ADMIN_TOKEN)
        self.assertEqual(status, 200)
        listed = json.dumps(body)
        # There can be neither plaintext tokens nor digests in the list - digests are equivalent to validators.
        self.assertNotIn("site_", listed)
        self.assertNotIn("token", listed)

    def test_a_disabled_tenant_loses_access_but_keeps_its_workloads(self) -> None:
        token = self.new_tenant("revoked-a")
        self.assertEqual(self.deploy(token, "web")[0], 202)
        self.assertEqual(
            self.call(
                "DELETE", self.scoped("/v1/tenants/revoked-a"), ADMIN_TOKEN
            )[0],
            202,
        )
        # The certificate becomes invalid immediately.
        self.assertEqual(self.call("GET", "/v1/deployments", token)[0], 401)
        # The workload is still there: revoking credentials does not mean deleting someone else’s stuff.
        self.assertTrue(
            any(
                (obj.get("spec") or {}).get("userID") == "revoked-a"
                for obj in Handler.kube.objects.values()
            )
        )

    def test_disabling_a_merchant_closes_both_credential_paths(self) -> None:
        """Deactivating a merchant must destroy both the merchant key and the tenant's own token.

        This corresponds to the equivalent of the two use cases at the tenant level at the merchant level. It was blank before: in 46 use cases
        None of them cover "whether a merchant's key can still be used after deactivating it." e2e was discovered only after it crashed out.

        The status codes returned by the two paths are different, and both are correct, so the assertion only pins "rejected":
        - Merchant's own key -> 401. SQL band for merchant_by_api_key
        `disabled_at IS NULL`, if the record cannot be found, it will fall into the general invalid service
        token, does not confirm to the holder that the key was ever valid - isomorphic to the tenant token.
        - Token of the tenant under your name -> 403 merchant is disabled. The caller has passed the authentication,
        Telling it the real reason will not lead people in the direction of "my token is wrong".
        Without any one of them, "deactivation" is only half-closed.
        """
        api_key = self.new_merchant("shuttered")

        status, tenant = self.call(
            "POST",
            "/v1/tenants",
            ADMIN_TOKEN,
            {"merchantId": "shuttered", "userId": KATE},
        )
        self.assertEqual(status, 201, tenant)
        tenant_token = tenant["token"]

        # Both the first two paths are open after deactivating, otherwise the following assertion may just be because it is not available at the beginning.
        self.assertEqual(
            self.call("GET", "/v1/deployments", api_key, subject=KATE)[0], 200
        )
        self.assertEqual(self.call("GET", "/v1/deployments", tenant_token)[0], 200)

        self.assertEqual(
            self.call("DELETE", "/v1/merchants/shuttered", ADMIN_TOKEN)[0], 202
        )

        key_status, _ = self.call("GET", "/v1/deployments", api_key, subject=KATE)
        self.assertEqual(key_status, 401)
        token_status, body = self.call("GET", "/v1/deployments", tenant_token)
        self.assertEqual(token_status, 403, body)
        self.assertIn("merchant", body.get("error", ""))

    def test_a_disabled_tenant_is_also_refused_while_acting_for_it(self) -> None:
        # Disabling has two paths and both must close at once. The tenant's own token is
        # filtered in SQL (`disabled_at IS NULL`); the merchant key acting for that subject
        # reaches the row by a different route entirely, so testing only the token path
        # looks conclusive while the common route stays open.
        api_key = self.new_merchant("revoked-house")
        acting = subject_for("revoked-a")
        # Forward comparison first: acting for this subject works before it is disabled,
        # otherwise the refusal below could simply be "it never worked".
        self.assertEqual(
            self.call("GET", "/v1/deployments", api_key, subject=acting)[0], 200
        )
        self.assertEqual(
            self.call(
                "DELETE",
                f"/v1/tenants/{acting}?merchantId=revoked-house",
                ADMIN_TOKEN,
            )[0],
            202,
        )
        status, body = self.call(
            "GET", "/v1/deployments", api_key, subject=acting
        )
        self.assertEqual(status, 403, body)
        self.assertIn("disabled", json.dumps(body))

        # A different subject under the same key is untouched - the gate is per tenant.
        live = subject_for("revoked-live")
        self.assertEqual(
            self.call("GET", "/v1/deployments", api_key, subject=live)[0], 200
        )

    def test_an_expired_merchant_key_is_refused_by_the_real_query(self) -> None:
        """The expiry is enforced by the same statement that resolves the key.

        Driven through PostgreSQL rather than a stand-in on purpose: the filter lives in
        the SQL, so a stub that reimplements it would keep answering correctly no matter
        what the statement said.
        """
        self.new_merchant("expiring-house", may_act=False)
        live = new_merchant_api_key()
        Handler.store.rotate_merchant_key(
            "expiring-house",
            token_digest(live),
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
        )
        # Forward comparison: a key inside its lifetime works, so the refusal below is
        # about the expiry and not about rotation having broken the key.
        self.assertEqual(self.call("GET", "/v1/deployments", live)[0], 200)

        expired = new_merchant_api_key()
        Handler.store.rotate_merchant_key(
            "expiring-house",
            token_digest(expired),
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1),
        )
        status, body = self.call("GET", "/v1/deployments", expired)
        # The same 401 as an unknown credential: answering "expired" would confirm that
        # this key digest once existed.
        self.assertEqual(status, 401, body)
        self.assertEqual(body["error"], "invalid service token")

    def test_a_key_without_the_grant_cannot_act_for_a_subject(self) -> None:
        """🔴 Contract §5.4: an unauthorized key that sends the header is refused, not ignored.

        Ignoring it would land the call on the key's own tenant and answer 2xx, so the
        caller would file one subject's resources under another and never learn.
        """
        api_key = self.new_merchant("no-grant-house", may_act=False)
        status, body = self.call(
            "GET", "/v1/deployments", api_key, subject=subject_for("someone")
        )
        self.assertEqual(status, 403, body)
        self.assertIn("not authorized to act", body["error"])
        # Forward comparison: the same key works for its own identity, so the refusal is
        # about acting rather than about the key being broken.
        self.assertEqual(self.call("GET", "/v1/deployments", api_key)[0], 200)

    def test_the_merchant_comes_from_the_credential_not_the_headers(self) -> None:
        """🔴 Contract §5.5. Two assertions, because either alone can pass while broken.

        The forged header is refused, **and** the same credential without it lands on the
        merchant the key belongs to - a request that merely 403s would also pass if the
        header were honoured somewhere else in the chain.
        """
        api_key = self.new_merchant("owner-house")
        self.new_merchant("victim-house")
        acting = subject_for("mover")
        for header in ("declared_merchant", "declared_user"):
            with self.subTest(header=header):
                status, body = self.call(
                    "GET",
                    "/v1/deployments",
                    api_key,
                    subject=acting,
                    **{header: "victim-house" if header == "declared_merchant" else "someone"},
                )
                self.assertEqual(status, 403, body)
        status, body = self.call(
            "GET", "/v1/deployments", api_key, subject=acting
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["merchantId"], "owner-house")

    def test_no_credential_is_refused_by_the_api_itself(self) -> None:
        # Contract §5.1: the refusal must come from this process, not from something in
        # front of it. This is a plain HTTP request to the bound socket with no headers.
        status, body = self.call("GET", "/v1/deployments", "")
        self.assertEqual(status, 401, body)

    def test_a_tenant_can_read_its_own_quota_only(self) -> None:
        token = self.new_tenant("self-a", maxDeployments=3, maxPublicRoutes=1)
        status, body = self.call("GET", "/v1/tenants/self", token)
        self.assertEqual(status, 200)
        self.assertEqual(body["userId"], "self-a")
        self.assertEqual(body["maxDeployments"], 3)

    def test_rotating_a_token_invalidates_the_old_one(self) -> None:
        token = self.new_tenant("rotate-a")
        self.assertEqual(self.call("GET", "/v1/tenants/self", token)[0], 200)

        status, body = self.call(
            "POST", self.scoped("/v1/tenants/rotate-a/token"), ADMIN_TOKEN
        )
        self.assertEqual(status, 200, body)
        fresh = body["token"]
        self.assertNotEqual(fresh, token)
        self.assertFalse(body["reenabled"])

        # The old token expires on the spot - exactly what you want after a leak.
        self.assertEqual(self.call("GET", "/v1/tenants/self", token)[0], 401)
        status, me = self.call("GET", "/v1/tenants/self", fresh)
        self.assertEqual(status, 200)
        self.assertEqual(me["userId"], "rotate-a")

    def test_a_disabled_tenant_can_be_brought_back_by_rotating(self) -> None:
        # Disabled only clears disabled_at, the record still occupies the only constraint of the name. If there is no such path,
        # A deactivated name is permanently unavailable: no name can be created, and there is no restoration entry.
        token = self.new_tenant("revive-a")
        self.assertEqual(
            self.call(
                "DELETE", self.scoped("/v1/tenants/revive-a"), ADMIN_TOKEN
            )[0],
            202,
        )
        self.assertEqual(self.call("GET", "/v1/tenants/self", token)[0], 401)

        # The reconstruction with the same name is still rejected, but the correct way out should be pointed out in the mistakes.
        status, body = self.call(
            "POST",
            "/v1/tenants",
            ADMIN_TOKEN,
            {"merchantId": DEFAULT_MERCHANT_ID, "userId": "revive-a"},
        )
        self.assertEqual(status, 409)
        self.assertIn("/token", body.get("hint", ""))

        status, body = self.call(
            "POST", self.scoped("/v1/tenants/revive-a/token"), ADMIN_TOKEN
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["reenabled"])
        revived = body["token"]
        self.assertEqual(self.call("GET", "/v1/tenants/self", revived)[0], 200)
        # After recovery, it can be deployed as usual.
        self.assertEqual(self.deploy(revived, "back")[0], 202)

    def test_only_admin_may_rotate_a_token(self) -> None:
        token = self.new_tenant("rotate-guard")
        status, _ = self.call(
            "POST", self.scoped("/v1/tenants/rotate-guard/token"), token
        )
        self.assertEqual(status, 403)

    def test_rotating_an_unknown_tenant_is_a_404(self) -> None:
        status, _ = self.call(
            "POST", self.scoped("/v1/tenants/nobody/token"), ADMIN_TOKEN
        )
        self.assertEqual(status, 404)

    # --- cross-tenant isolation ----------------------------------------
    def test_a_tenant_cannot_see_another_tenants_deployments(self) -> None:
        a = self.new_tenant("iso-a")
        b = self.new_tenant("iso-b")
        self.assertEqual(self.deploy(a, "secret-app")[0], 202)

        status, body = self.call("GET", "/v1/deployments", b)
        self.assertEqual(status, 200)
        self.assertEqual(body["deployments"], [])
        # You can't get it even if you call the name directly.
        self.assertEqual(
            self.call("GET", "/v1/deployments/secret-app", b)[0], 404
        )

    def test_a_tenant_cannot_delete_another_tenants_deployment(self) -> None:
        a = self.new_tenant("del-a")
        b = self.new_tenant("del-b")
        self.assertEqual(self.deploy(a, "victim")[0], 202)

        status, _ = self.call("DELETE", "/v1/deployments/victim", b)
        self.assertEqual(status, 202)  # Idempotent: does not exist for b
        # The deployment of a must still be there as is.
        self.assertEqual(
            self.call("GET", "/v1/deployments/victim", a)[0], 200
        )

    def test_a_crafted_name_cannot_overwrite_another_tenant(self) -> None:
        # cr_name used to be dns_label(f"{user}-{service}"), so acme-corp/web is the same as
        # acme/corp-web will get the same CR name - cross-tenant coverage.
        long_tenant = self.new_tenant("acme-corp")
        short_tenant = self.new_tenant("acme")
        self.assertEqual(self.deploy(long_tenant, "web")[0], 202)
        self.assertEqual(self.deploy(short_tenant, "corp-web")[0], 202)

        owners = {
            name: (obj.get("spec") or {}).get("userID")
            for name, obj in Handler.kube.objects.items()
        }
        self.assertEqual(len(owners), 2, owners)
        self.assertEqual(set(owners.values()), {"acme-corp", "acme"})

    def test_a_tenant_token_cannot_impersonate_another(self) -> None:
        a = self.new_tenant("imp-a")
        self.new_tenant("imp-b")
        status, _ = self.call(
            "GET", "/v1/deployments", a, declared_user="imp-b"
        )
        self.assertEqual(status, 403)

    def test_a_merchant_key_cannot_reach_the_same_name_next_door(self) -> None:
        """Cross-merchant override, use true HTTP + true authentication instead of directly adjusting the store.

        Why is it necessary to test like this: user_id is only unique within a merchant, and two merchants can each have one called shared
        Tenant - whether the isolation is established depends on whether the server has the requester's own merchant_id
        Bring in query. The rest of the cross-merchant assertions directly call store.*, bypassing the entire _authenticate section, and
        The entrance to isolation is right there. Both merchants on this street are really
        built and both have a real tenant of that name, so the isolation is what is being
        measured rather than the absence of one side.

        The forward comparison must be run first: first prove that B can actually get the deployment with his own credentials, and then assert that A can get it
        Less than. Without the first half, once WHERE degenerates into a constant void, this use case will be falsely green.
        """
        key_a = self.new_merchant("xmerchant-a")
        key_b = self.new_merchant("xmerchant-b")
        for merchant in ("xmerchant-a", "xmerchant-b"):
            status, body = self.call(
                "POST",
                "/v1/tenants",
                ADMIN_TOKEN,
                {"merchantId": merchant, "userId": SHARED},
            )
            self.assertEqual(status, 201, body)

        status, created = self.call(
            "POST",
            "/v1/deployments",
            key_b,
            {
                "name": "ledger",
                "image": "example.invalid/app:v1",
                "port": 8080,
                "healthPath": "/",
            },
            subject=SHARED,
        )
        self.assertEqual(status, 202, created)
        self.assertEqual(created["merchantId"], "xmerchant-b")

        # Positive comparison.
        status, mine = self.call("GET", "/v1/deployments", key_b, subject=SHARED)
        self.assertEqual(status, 200, mine)
        self.assertEqual(mine["merchantId"], "xmerchant-b")
        self.assertEqual(
            [row["serviceName"] for row in mine["deployments"]], ["ledger"]
        )
        self.assertEqual(
            self.call("GET", "/v1/deployments/ledger", key_b, subject=SHARED)[0],
            200,
        )

        # A's key is substituted into the tenant with the same name: authentication will be released (it is substituted into a shared under its own name, which does not exist)
        # CCB on the spot), but what you see must be your own empty world.
        status, crossed = self.call(
            "GET", "/v1/deployments", key_a, subject=SHARED
        )
        self.assertEqual(status, 200, crossed)
        self.assertEqual(crossed["merchantId"], "xmerchant-a")
        self.assertEqual(crossed["deployments"], [])
        self.assertEqual(
            self.call("GET", "/v1/deployments/ledger", key_a, subject=SHARED)[0],
            404,
        )

        # It cannot be deleted even if it is deleted: for A, this item does not exist in the first place, and it is idempotent 202; for B, it must remain intact.
        self.assertEqual(
            self.call(
                "DELETE", "/v1/deployments/ledger", key_a, subject=SHARED
            )[0],
            202,
        )
        self.assertEqual(
            self.call("GET", "/v1/deployments/ledger", key_b, subject=SHARED)[0],
            200,
        )

    # --- quotas ---------------------------------------------------------
    def test_the_deployment_quota_is_enforced_per_tenant(self) -> None:
        a = self.new_tenant("quota-a", maxDeployments=2, maxPublicRoutes=2)
        b = self.new_tenant("quota-b", maxDeployments=2, maxPublicRoutes=2)
        self.assertEqual(self.deploy(a, "one")[0], 202)
        self.assertEqual(self.deploy(a, "two")[0], 202)

        status, body = self.deploy(a, "three")
        self.assertEqual(status, 429)
        self.assertEqual(body["code"], "quota_exceeded")
        # Other people's quotas are not affected.
        self.assertEqual(self.deploy(b, "one")[0], 202)

    def test_redeploying_the_same_service_does_not_consume_quota(self) -> None:
        a = self.new_tenant("redeploy-a", maxDeployments=1, maxPublicRoutes=1)
        self.assertEqual(self.deploy(a, "web")[0], 202)
        self.assertEqual(self.deploy(a, "web")[0], 202)

    def test_the_public_route_quota_is_enforced(self) -> None:
        a = self.new_tenant("route-a", maxDeployments=5, maxPublicRoutes=1)
        self.assertEqual(self.deploy(a, "site")[0], 202)
        status, body = self.deploy(a, "second-site")
        self.assertEqual(status, 429, body)
        # internal does not occupy public routes.
        self.assertEqual(
            self.deploy(a, "worker", exposure="internal")[0], 202
        )

    # --- port pool ------------------------------------------------------
    @_POOL_ONLY
    def test_each_public_service_gets_a_distinct_pool_port(self) -> None:
        ports = []
        for index in range(3):
            token = self.new_tenant(f"port-{index}")
            self.assertEqual(self.deploy(token, f"site-{index}")[0], 202)
        for obj in Handler.kube.objects.values():
            spec = obj.get("spec") or {}
            if spec.get("exposure") == "public":
                ports.append(int(spec["nodePort"]))
        self.assertEqual(len(ports), 3)
        self.assertEqual(len(set(ports)), 3, f"Duplicate port allocation: {ports}")
        self.assertTrue(set(ports) <= set(NODE_PORT_RANGE))

    @_POOL_ONLY
    def test_the_caller_cannot_choose_its_own_port(self) -> None:
        token = self.new_tenant("chooser")
        status, _ = self.deploy(token, "site")
        self.assertEqual(status, 202)
        spec = next(iter(Handler.kube.objects.values()))["spec"]
        # Submissions to nodePort do not count: the control plane is overwritten in-place with the one assigned by the pool.
        self.assertIn(int(spec["nodePort"]), NODE_PORT_RANGE)

    @_POOL_ONLY
    def test_a_quota_cannot_be_larger_than_the_pool(self) -> None:
        pool = len(NODE_PORT_RANGE)
        status, body = self.call(
            "POST",
            "/v1/tenants",
            ADMIN_TOKEN,
            {
                "merchantId": DEFAULT_MERCHANT_ID,
                "userId": "greedy",
                "maxPublicRoutes": pool + 1,
            },
        )
        self.assertEqual(status, 400, body)

    @_POOL_ONLY
    def test_an_exhausted_pool_is_refused_with_a_clear_reason(self) -> None:
        # Each of the two tenants is within their own quota, but together they fill the pool. Reason for rejection at this time
        # Must be a pool and not a quota - both mean completely different next steps for the caller.
        pool = len(NODE_PORT_RANGE)
        hog = self.new_tenant(
            "pool-hog", maxDeployments=pool, maxPublicRoutes=pool
        )
        for index in range(pool):
            self.assertEqual(self.deploy(hog, f"site-{index}")[0], 202)

        late = self.new_tenant(
            "pool-late", maxDeployments=2, maxPublicRoutes=1
        )
        status, body = self.deploy(late, "late-site")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "public_route_capacity")
        # Internal services are not affected by the pool and can still be deployed.
        self.assertEqual(
            self.deploy(late, "late-worker", exposure="internal")[0], 202
        )


# ---------------------------------------------------------------------------
# Multi-merchant: Named derivation and storage tiers. This group does not go through HTTP - they are targeting (merchant, user)
# This binary identity holds at both derivation and storage, rather than the behavior of an endpoint.
# ---------------------------------------------------------------------------

def _deployment_cr(merchant_id: str, user_id: str, service_name: str) -> dict:
    return site_deployment_resource(
        {
            "name": service_name,
            "image": "example.invalid/app:v1",
            "port": 8080,
            "healthPath": "/",
        },
        merchant_id,
        user_id,
    )


class TenantNamingTests(unittest.TestCase):
    """The derivation of (merchant, user) to K8s name must be injective.

    This is not a matter of "nice naming": the Namespace is the isolation boundary itself, and the CR name is unique in the cluster. two
    If different (merchant, user) names fall into the same name, it is a cross-merchant coverage/reading path.
    """

    def test_a_merchant_and_user_boundary_cannot_be_shifted(self) -> None:
        # Readable prefixes will be bumped into the same string - this is exactly how "spelling string" derivation fails. Uniqueness must
        # Guaranteed by the digest, so the two full names remain different.
        self.assertNotEqual(
            namespace_for_tenant("a-ub", "c"), namespace_for_tenant("a", "b-uc")
        )
        self.assertNotEqual(
            cr_name_for("a-ub", "c", "d"), cr_name_for("a", "b-uc", "d")
        )
        self.assertNotEqual(
            cr_name_for("a", "b", "uc-d"), cr_name_for("a", "b-uc", "d")
        )
        self.assertNotEqual(
            cr_name_for("acme-corp", "web", "api"),
            cr_name_for("acme", "corp-web", "api"),
        )

    def test_the_derivation_is_stable_and_fits_a_dns_label(self) -> None:
        self.assertEqual(
            namespace_for_tenant("acme", "alice"),
            namespace_for_tenant("acme", "alice"),
        )
        # Upper limit: 31 characters merchant + 63 characters tenant + 63 characters service name, still must fall within 63.
        self.assertLessEqual(len(namespace_for_tenant("m" * 31, "u" * 63)), 63)
        self.assertLessEqual(
            len(cr_name_for("m" * 31, "u" * 63, "s" * 63)), 63
        )

    def test_the_same_user_name_under_two_merchants_is_two_namespaces(self) -> None:
        self.assertNotEqual(
            namespace_for_tenant("merchant-a", "alice"),
            namespace_for_tenant("merchant-b", "alice"),
        )


class _RecordingCursor:
    """A cursor that records SQL without executing it, used to see what statements will be issued in the PostgreSQL dialect.

    Boundaries: This is not "something that works on PostgreSQL". This machine only has a client and no server, so this
    These tests pin PostgreSQL placeholders and migration statement shape.
    Does table modification follow the unified path of "create new table → copy → delete old table → rename"?
    """

    def __init__(
        self,
        columns: dict[str, tuple[str, ...]] | None = None,
        *,
        tables: tuple[str, ...] = (),
        versions: tuple[int, ...] = (),
    ) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self._columns = columns if columns is not None else {}
        self._tables = frozenset(tables)
        self._versions = tuple(versions)
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((" ".join(sql.split()), tuple(params)))
        text = " ".join(sql.split())
        if "information_schema.tables" in text:
            # The list of table names is in the IN (...) literal, without placeholders - identify the candidates from the statement
            # Come out, and then find the intersection of the configured tables to simulate "which tables actually exist in the library."
            wanted = [
                name.strip().strip("'")
                for name in text.rsplit("IN (", 1)[-1].rstrip(")").split(",")
            ]
            self._rows = [(name,) for name in wanted if name in self._tables]
        elif "information_schema.columns" in text:
            self._rows = [(name,) for name in self._columns.get(params[0], ())]
        elif text.startswith("SELECT version FROM"):
            self._rows = [(version,) for version in self._versions]
        elif text.startswith("SELECT COUNT(*)"):
            self._rows = [(0,)]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class PostgresDialectMigrationTests(unittest.TestCase):
    _LEGACY_DEPLOYMENT_COLUMNS = (
        "user_id", "service_name", "cr_name", "image", "port", "health_path",
        "revision", "spec", "phase", "message", "url", "created_at",
        "updated_at", "deletion_requested_at", "deleted_at",
    )
    _LEGACY_TENANT_COLUMNS = (
        "user_id", "token_sha256", "max_deployments", "max_public_routes",
        "created_at", "disabled_at",
    )

    def _run(self) -> _RecordingCursor:
        cursor = _RecordingCursor(
            {
                "sites_deployments": self._LEGACY_DEPLOYMENT_COLUMNS,
                "sites_tenants": self._LEGACY_TENANT_COLUMNS,
            }
        )
        store = Store(_POSTGRES, lambda: None)
        store._migrate_multi_merchant(cursor)
        return cursor

    def test_the_postgres_path_never_emits_sqlite_syntax(self) -> None:
        statements = [sql for sql, _ in self._run().statements]
        joined = "\n".join(statements)
        self.assertNotIn("PRAGMA", joined)
        self.assertNotIn("sqlite_master", joined)
        self.assertNotIn("?", joined)
        self.assertIn("information_schema.columns", joined)

    def test_both_tables_are_rebuilt_through_the_same_shadow_path(self) -> None:
        statements = [sql for sql, _ in self._run().statements]
        for table in ("sites_deployments", "sites_tenants"):
            shadow = f"{table}_new"
            steps = [
                f"DROP TABLE IF EXISTS {shadow}",
                f"CREATE TABLE IF NOT EXISTS {shadow} (",
                f"INSERT INTO {shadow} (",
                f"DROP TABLE {table}",
                f"ALTER TABLE {shadow} RENAME TO {table}",
            ]
            positions = []
            for step in steps:
                matches = [i for i, sql in enumerate(statements) if step in sql]
                self.assertEqual(len(matches), 1, f"{step!r} in {statements}")
                positions.append(matches[0])
            self.assertEqual(positions, sorted(positions), table)

    def test_copied_rows_are_bound_to_the_default_merchant(self) -> None:
        copies = [
            (sql, params)
            for sql, params in self._run().statements
            if sql.startswith("INSERT INTO sites_")
        ]
        self.assertEqual(len(copies), 2, copies)
        for sql, params in copies:
            self.assertEqual(params, (DEFAULT_MERCHANT_ID,))
            self.assertIn("SELECT %s,", sql)

    def test_an_already_migrated_database_is_left_alone(self) -> None:
        cursor = _RecordingCursor(
            {
                "sites_deployments": self._LEGACY_DEPLOYMENT_COLUMNS
                + ("merchant_id",),
                "sites_tenants": self._LEGACY_TENANT_COLUMNS + ("merchant_id",),
            }
        )
        Store(_POSTGRES, lambda: None)._migrate_multi_merchant(cursor)
        self.assertEqual(
            [sql for sql, _ in cursor.statements if "information_schema" not in sql],
            [],
        )


class SchemaVersioningPostgresDialectTests(unittest.TestCase):
    """Statements issued by the sequential migration mechanism (version table + sequential replacement) in the PG dialect.

    Same boundary as PostgresDialectMigrationTests: pin statement text only - version table vs version
    Record whether there are dialect templates and whether the applied steps will replay the DDL.
    """

    def _migrate(self, cursor: _RecordingCursor) -> None:
        Store(_POSTGRES, lambda: _FakeConnection(cursor)).migrate()

    def test_a_fresh_database_renders_the_whole_mechanism_through_the_dialect(
        self,
    ) -> None:
        cursor = _RecordingCursor({})
        self._migrate(cursor)
        joined = "\n".join(sql for sql, _ in cursor.statements)
        # Migration statements must remain valid PostgreSQL.
        self.assertNotIn("PRAGMA", joined)
        self.assertNotIn("sqlite_master", joined)
        self.assertNotIn("?", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS sites_schema_migrations", joined)
        # The column type and default time of the version table must also be specified: TIMESTAMPTZ/NOW(), instead of
        # PostgreSQL uses native JSONB and TIMESTAMPTZ types.
        self.assertIn("applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()", joined)
        records = [
            (sql, params)
            for sql, params in cursor.statements
            if sql.startswith("INSERT INTO sites_schema_migrations")
        ]
        self.assertEqual(len(records), 6, cursor.statements)
        self.assertEqual([record[1][0] for record in records], [1, 2, 3, 4, 5, 6])
        self.assertIn("ON CONFLICT (version) DO NOTHING", records[0][0])
        # The steps of v1 were actually executed before the version record was written.
        self.assertIn("CREATE TABLE IF NOT EXISTS sites_merchants", joined)
        self.assertLess(
            joined.index("CREATE TABLE IF NOT EXISTS sites_merchants"),
            joined.index("INSERT INTO sites_schema_migrations"),
        )

    def test_an_applied_version_is_not_replayed_on_postgres(self) -> None:
        cursor = _RecordingCursor(
            {}, tables=("sites_schema_migrations",), versions=(1, 2, 3, 4, 5, 6)
        )
        self._migrate(cursor)
        joined = "\n".join(sql for sql, _ in cursor.statements)
        self.assertIn("SELECT version FROM sites_schema_migrations", joined)
        # There is no replay of the applied steps: rename, shadow table, table creation, version record, none.
        self.assertNotIn("ALTER TABLE appforge", joined)
        self.assertNotIn("_new", joined)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS sites_merchants", joined)
        self.assertNotIn("INSERT INTO sites_schema_migrations", joined)


# The other half of the migration is on PostgreSQL. The production control plane is PG, and "create new table → copy → delete old table → rename"
# Only run once at startup, an error is data corruption - statement level assertion (PostgresDialectMigrationTests)
# It can block placeholders and dialect syntax, but cannot block "PG does not accept this statement".
#
# This class only runs when SITES_TEST_PG_DSN is set, and it must point at a database
# that is safe to destroy: every case starts by dropping and recreating "public".
# Never point it at SITES_DB_HOST or any other production setting.
#
# The DSN needs an explicit sslmode. DatabaseConfig defaults to "require" -- that is
# deliberate for production -- and a throwaway local container speaks no TLS, so
# omitting it fails every case with "server does not support SSL, but SSL was required".
#
# Locally, `make test-db && make test` sets all of this up; the Makefile's default DSN
# already carries sslmode=disable. To run just this class against that container:
#
#   SITES_TEST_PG_DSN='host=127.0.0.1 port=55439 dbname=postgres user=postgres sslmode=disable' \
#     python3 -m unittest tests.test_tenancy.PostgresMigrationTests
#
# In CI, the python job sets SITES_TEST_PG_DSN as job-level env, so these cases run as
# part of the normal suite, and the "Assert the PostgreSQL migration tests are not
# skipped" step re-runs the class and fails on any skip. That guard exists because
# unittest reports a fully skipped class as OK: before it was added, this class had
# never run in CI at all while CI stayed green.
_PG_DSN = os.environ.get("SITES_TEST_PG_DSN", "")

_PG_LEGACY_SCHEMA = (
    """
    CREATE TABLE sites_deployments (
        user_id TEXT NOT NULL,
        service_name TEXT NOT NULL,
        cr_name TEXT NOT NULL UNIQUE,
        image TEXT NOT NULL,
        port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
        health_path TEXT NOT NULL,
        revision TEXT NOT NULL,
        spec JSONB NOT NULL,
        phase TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        url TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deletion_requested_at TIMESTAMPTZ,
        deleted_at TIMESTAMPTZ,
        PRIMARY KEY (user_id, service_name)
    )
    """,
    "CREATE INDEX sites_deployments_phase_idx ON sites_deployments (phase)",
    """
    CREATE TABLE sites_tenants (
        user_id TEXT PRIMARY KEY,
        token_sha256 TEXT NOT NULL UNIQUE,
        max_deployments INTEGER NOT NULL,
        max_public_routes INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        disabled_at TIMESTAMPTZ
    )
    """,
)


@unittest.skipUnless(_PG_DSN, "SITES_TEST_PG_DSN is not set (see the note above)")
class PostgresMigrationTests(unittest.TestCase):
    """The same migration, on real PostgreSQL.

    Each use case begins with DROP SCHEMA public CASCADE, so this DSN can only point to libraries that are thrown away after use.
    """

    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")

    def _store(self):
        from sites.storage import DatabaseConfig

        parts = self.psycopg.conninfo.conninfo_to_dict(_PG_DSN)
        return Store.postgres(
            DatabaseConfig(
                host=parts.get("host", "127.0.0.1"),
                port=int(parts.get("port", 5432)),
                dbname=parts.get("dbname", "sites"),
                user=parts.get("user", "postgres"),
                password=parts.get("password", ""),
                # conninfo carries sslmode; dropping it here silently falls back to
                # the production default of "require", which no plain local test
                # container speaks. That turns `make test` red on a correct setup.
                sslmode=parts.get("sslmode", "require"),
            )
        )

    def _query(self, sql: str) -> list[tuple]:
        with self.psycopg.connect(_PG_DSN, autocommit=True) as connection:
            return list(connection.execute(sql))

    def _build_legacy(self, *, active_deployments: int = 0) -> None:
        with self.psycopg.connect(_PG_DSN, autocommit=True) as connection:
            for statement in _PG_LEGACY_SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO sites_tenants (user_id, token_sha256,"
                " max_deployments, max_public_routes, created_at, disabled_at)"
                " VALUES ('alice','digest-alice',5,1,"
                "'2026-01-01 00:00:00+00',NULL),"
                " ('bob','digest-bob',7,2,'2026-01-02 00:00:00+00',NULL),"
                " ('carol','digest-carol',3,1,'2026-01-03 00:00:00+00',"
                "'2026-02-01 00:00:00+00')"
            )
            connection.execute(
                "INSERT INTO sites_deployments (user_id, service_name, cr_name,"
                " image, port, health_path, revision, spec, phase, message,"
                " url, created_at, updated_at, deletion_requested_at,"
                " deleted_at) VALUES ('bob','api','legacy-bob-api',"
                "'example.invalid/app:v2',9000,'/healthz','3','{}'::jsonb,"
                "'Deleted','','http://127.0.0.1:18090/',"
                "'2026-01-06 00:00:00+00','2026-01-07 00:00:00+00',"
                "'2026-01-07 00:00:00+00','2026-01-07 00:00:00+00')"
            )
            for index in range(active_deployments):
                connection.execute(
                    "INSERT INTO sites_deployments (user_id, service_name,"
                    " cr_name, image, port, health_path, revision, spec, phase,"
                    " message, url) VALUES ('alice', %s, %s,"
                    "'example.invalid/app:v1',8080,'/','1','{}'::jsonb,"
                    "'Ready','',NULL)",
                    (f"live-{index}", f"legacy-alice-live-{index}"),
                )

    def _primary_key(self, table: str) -> list[str]:
        rows = self._query(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            f"WHERE i.indrelid = '{table}'::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
        return [row[0] for row in rows]

    def _columns(self, table: str) -> set[str]:
        return {
            row[0]
            for row in self._query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                f"AND table_name = '{table}'"
            )
        }

    def test_every_row_survives_on_postgres(self) -> None:
        self._build_legacy()
        store = self._store()
        store.migrate()

        tenants = store.list_tenants()
        self.assertEqual(len(tenants), 3)
        self.assertEqual(
            {row["merchant_id"] for row in tenants}, {DEFAULT_MERCHANT_ID}
        )
        by_id = {row["user_id"]: row for row in tenants}
        self.assertEqual(by_id["bob"]["token_sha256"], "digest-bob")
        self.assertEqual(by_id["bob"]["max_deployments"], 7)
        # created_at must be the original value: copying missing columns will fall into DEFAULT NOW(), which will cause damage afterwards
        # Can't tell at all. What PG retrieves is datetime, which is more critical than the date part.
        self.assertEqual(by_id["bob"]["created_at"].date().isoformat(), "2026-01-02")
        self.assertIsNotNone(by_id["carol"]["disabled_at"])
        self.assertIsNone(by_id["alice"]["disabled_at"])

        bob = store.get_deployment(DEFAULT_MERCHANT_ID, "bob", "api")
        self.assertEqual(bob["cr_name"], "legacy-bob-api")
        self.assertEqual(bob["port"], 9000)
        self.assertEqual(bob["url"], "http://127.0.0.1:18090/")
        # The four timestamps of the deployment table must also be pinned one by one, not just the tenant table: two tables are two INSERTs
        # ... SELECT, missing a column will only destroy one of them. PG's DEFAULT NOW() will replace the missing columns
        # The migration moment is silently completed and cannot be seen from the data afterwards.
        self.assertEqual(bob["created_at"].date().isoformat(), "2026-01-06")
        self.assertEqual(bob["updated_at"].date().isoformat(), "2026-01-07")
        self.assertEqual(
            bob["deletion_requested_at"].date().isoformat(), "2026-01-07"
        )
        self.assertEqual(bob["deleted_at"].date().isoformat(), "2026-01-07")

    def test_the_rebuilt_tables_have_the_new_shape_on_postgres(self) -> None:
        self._build_legacy()
        self._store().migrate()

        self.assertEqual(
            self._primary_key("sites_deployments"),
            ["merchant_id", "user_id", "service_name"],
        )
        self.assertEqual(
            self._primary_key("sites_tenants"), ["merchant_id", "user_id"]
        )
        indexes = {
            row[0]
            for row in self._query(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema()"
            )
        }
        self.assertIn("sites_deployments_phase_idx", indexes)
        self.assertIn("sites_tenants_merchant_idx", indexes)
        # The shadow table must have been renamed and left, and cannot remain in the library.
        self.assertEqual(
            [
                row[0]
                for row in self._query(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = current_schema() "
                    "AND tablename LIKE '%\\_new'"
                )
            ],
            [],
        )

    def test_the_migration_is_idempotent_on_postgres(self) -> None:
        self._build_legacy()
        store = self._store()
        store.migrate()
        first = store.list_tenants()
        key = store.merchant(DEFAULT_MERCHANT_ID)["api_key_sha256"]

        store.migrate()

        self.assertEqual(store.list_tenants(), first)
        self.assertEqual(
            store.merchant(DEFAULT_MERCHANT_ID)["api_key_sha256"], key
        )

    def test_an_active_deployment_refuses_the_migration_on_postgres(self) -> None:
        self._build_legacy(active_deployments=2)
        with self.assertRaises(StorageError) as caught:
            self._store().migrate()
        message = str(caught.exception)
        self.assertIn("2 active deployment(s)", message)
        self.assertIn("cr_name", message)
        # Rejection must occur before taking action: both tables are still in the old structure, and there are no shadow tables left.
        self.assertNotIn("merchant_id", self._columns("sites_tenants"))
        self.assertNotIn("merchant_id", self._columns("sites_deployments"))
        self.assertEqual(
            [
                row[0]
                for row in self._query(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = current_schema() "
                    "AND tablename LIKE '%\\_new'"
                )
            ],
            [],
        )

    def test_tenants_stay_isolated_after_migrating_on_postgres(self) -> None:
        self._build_legacy()
        store = self._store()
        store.migrate()
        store.create_merchant(
            "acme", "Acme", token_digest("key-acme"), 10, 20
        )
        store.create_tenant(
            "acme", "alice", token_digest("token-acme-alice"),
            max_deployments=5, max_public_routes=1,
        )

        # The local/alice brought by the migration and the newly created acme/alice are two lines.
        self.assertNotEqual(
            store.tenant("acme", "alice")["token_sha256"],
            store.tenant(DEFAULT_MERCHANT_ID, "alice")["token_sha256"],
        )
        store.update_tenant_quota("acme", "alice", max_deployments=9)
        self.assertEqual(store.tenant("acme", "alice")["max_deployments"], 9)
        # The tenant with the same name is under the name of another merchant and cannot be changed incidentally.
        self.assertEqual(
            store.tenant(DEFAULT_MERCHANT_ID, "alice")["max_deployments"], 5
        )

        store.upsert_site_deployment(_deployment_cr("acme", "alice", "web"))
        self.assertEqual(
            [row["service_name"] for row in store.list_deployments("acme", "alice")],
            ["web"],
        )
        self.assertIsNone(
            store.get_deployment(DEFAULT_MERCHANT_ID, "alice", "web")
        )
        self.assertEqual(store.count_deployments_by_merchant(), {"acme": 1})


class MerchantIsolationTests(unittest.TestCase):
    """Two merchants each have a tenant named alice, and neither can see anything from the other."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = postgres_store(Path(directory.name) / "sites.db")
        self.store.migrate()
        self.tokens = {}
        for merchant in ("merchant-a", "merchant-b"):
            self.store.create_merchant(
                merchant, f"Merchant {merchant[-1].upper()}",
                token_digest(f"key-{merchant}"), 10, 20,
            )
            token = new_tenant_token()
            self.tokens[merchant] = token
            self.store.create_tenant(
                merchant, "alice", token_digest(token),
                max_deployments=5, max_public_routes=1,
            )
            self.store.upsert_site_deployment(
                _deployment_cr(merchant, "alice", f"site-{merchant[-1]}")
            )

    def test_the_same_user_name_is_two_independent_rows(self) -> None:
        first = self.store.tenant("merchant-a", "alice")
        second = self.store.tenant("merchant-b", "alice")
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertNotEqual(first["token_sha256"], second["token_sha256"])
        self.assertEqual(first["merchant_id"], "merchant-a")
        self.assertEqual(second["merchant_id"], "merchant-b")
        self.assertNotEqual(
            namespace_for_tenant("merchant-a", "alice"),
            namespace_for_tenant("merchant-b", "alice"),
        )

    def test_a_token_resolves_to_its_own_merchant(self) -> None:
        for merchant, token in self.tokens.items():
            row = self.store.tenant_by_token(token_digest(token))
            self.assertEqual(row["merchant_id"], merchant)

    def test_neither_merchant_can_read_the_others_deployments(self) -> None:
        listed = self.store.list_deployments("merchant-a", "alice")
        self.assertEqual([row["service_name"] for row in listed], ["site-a"])
        self.assertEqual(listed[0]["merchant_id"], "merchant-a")
        # The name is correct but the merchant is wrong -> cannot be found. Without the merchant condition, the other party's row will be returned.
        self.assertIsNone(
            self.store.get_deployment("merchant-b", "alice", "site-a")
        )
        self.assertIsNone(
            self.store.get_deployment("merchant-a", "alice", "site-b")
        )

    def test_a_status_update_cannot_cross_a_merchant_boundary(self) -> None:
        self.store.set_status(
            "merchant-b", "alice", "site-a", "Ready", "should not apply"
        )
        self.assertEqual(
            self.store.get_deployment("merchant-a", "alice", "site-a")["phase"],
            "Pending",
        )

    def test_listing_tenants_can_be_scoped_to_one_merchant(self) -> None:
        self.assertEqual(len(self.store.list_tenants()), 2)
        scoped = self.store.list_tenants(merchant_id="merchant-a")
        self.assertEqual([row["merchant_id"] for row in scoped], ["merchant-a"])
        self.assertEqual(self.store.count_tenants("merchant-b"), 1)

    def test_the_admin_aggregate_spans_merchants_and_filters(self) -> None:
        everything = self.store.list_all_deployments()
        self.assertEqual(
            {row["merchant_id"] for row in everything},
            {"merchant-a", "merchant-b"},
        )
        scoped = self.store.list_all_deployments(merchant_id="merchant-b")
        self.assertEqual([row["service_name"] for row in scoped], ["site-b"])
        self.assertEqual(self.store.list_all_deployments(phase="Ready"), [])
        self.assertEqual(
            self.store.count_deployments_by_merchant(),
            {"merchant-a": 1, "merchant-b": 1},
        )

    def test_disabling_a_merchant_kills_its_key_but_keeps_the_record(self) -> None:
        digest = token_digest("key-merchant-a")
        self.assertIsNotNone(self.store.merchant_by_api_key(digest))
        self.store.disable_merchant("merchant-a")
        self.assertIsNone(self.store.merchant_by_api_key(digest))
        # The management end must still be able to see it, otherwise deactivation is equivalent to disappearing from the console and can never be restored.
        disabled = self.store.merchant("merchant-a")
        self.assertIsNotNone(disabled["disabled_at"])
        self.assertIn(
            "merchant-a", [row["merchant_id"] for row in self.store.list_merchants()]
        )
        self.store.rotate_merchant_key("merchant-a", token_digest("key-new"))
        self.assertIsNone(self.store.merchant("merchant-a")["disabled_at"])
        self.assertIsNone(self.store.merchant_by_api_key(digest))
        self.assertIsNotNone(
            self.store.merchant_by_api_key(token_digest("key-new"))
        )

    def test_a_merchant_patch_touches_only_the_fields_it_was_given(self) -> None:
        self.store.update_merchant_quota("merchant-a", max_tenants=42)
        row = self.store.merchant("merchant-a")
        self.assertEqual(row["max_tenants"], 42)
        # Fields not given must be left as is - PATCH is not PUT.
        self.assertEqual(row["max_deployments"], 20)
        self.assertEqual(row["display_name"], "Merchant A")

        self.store.update_merchant_quota("merchant-a", max_deployments=99)
        row = self.store.merchant("merchant-a")
        self.assertEqual(row["max_deployments"], 99)
        self.assertEqual(row["max_tenants"], 42)

        # An empty PATCH does not succeed silently: the caller responds with a 400 instead of "fixed".
        with self.assertRaises(ValueError):
            self.store.update_merchant_quota("merchant-a")
        # Changing one merchant cannot affect another.
        self.assertEqual(
            self.store.merchant("merchant-b")["max_tenants"], 10
        )

    def test_renaming_a_merchant_leaves_its_quota_and_key_alone(self) -> None:
        before = self.store.merchant("merchant-a")
        self.store.update_merchant_display_name("merchant-a", "Renamed")
        after = self.store.merchant("merchant-a")
        self.assertEqual(after["display_name"], "Renamed")
        # Renaming is a purely display operation: quota, certificate summary, and deactivation status cannot be changed incidentally.
        for column in ("max_tenants", "max_deployments", "api_key_sha256",
                       "created_at", "disabled_at"):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual(
            self.store.merchant("merchant-b")["display_name"], "Merchant B"
        )
        # The name does not participate in any derivation, so you can still find it by id after changing it.
        self.assertEqual(after["merchant_id"], "merchant-a")

    def test_a_tenant_patch_touches_only_that_merchants_row(self) -> None:
        self.store.update_tenant_quota("merchant-a", "alice", max_deployments=9)
        first = self.store.tenant("merchant-a", "alice")
        self.assertEqual(first["max_deployments"], 9)
        self.assertEqual(first["max_public_routes"], 1)
        # The tenant with the same name is under the name of another merchant and cannot be changed incidentally.
        self.assertEqual(
            self.store.tenant("merchant-b", "alice")["max_deployments"], 5
        )

        self.store.update_tenant_quota(
            "merchant-b", "alice", max_public_routes=4
        )
        second = self.store.tenant("merchant-b", "alice")
        self.assertEqual(second["max_public_routes"], 4)
        self.assertEqual(second["max_deployments"], 5)

        with self.assertRaises(ValueError):
            self.store.update_tenant_quota("merchant-a", "alice")

    def test_the_snapshot_reconciles_each_merchant_independently(self) -> None:
        # The main write path of the operator: CR upsert if it is still in the cluster, soft delete if it is not there. CR name is global
        # Unique, so the recycling is based on cr_name - but the records of the two merchants must be settled separately, one side
        # The disappearance of CR cannot mark the record on the other side as deleted.
        surviving = _deployment_cr("merchant-a", "alice", "site-a")
        self.store.sync_snapshot([surviving])

        alive = self.store.get_deployment("merchant-a", "alice", "site-a")
        self.assertIsNone(alive["deleted_at"])
        gone = self.store.get_deployment("merchant-b", "alice", "site-b")
        self.assertIsNotNone(gone["deleted_at"])
        self.assertEqual(gone["phase"], "Deleted")
        self.assertEqual(
            self.store.count_deployments_by_merchant(), {"merchant-a": 1}
        )

    def test_a_duplicate_user_name_under_one_merchant_is_still_refused(self) -> None:
        with self.assertRaises(StorageError):
            self.store.create_tenant(
                "merchant-a", "alice", token_digest(new_tenant_token()),
                max_deployments=1, max_public_routes=1,
            )


class SnapshotFaultToleranceTests(unittest.TestCase):
    """A bad CR cannot freeze the entire platform reconciliation.

    Snapshot convergence is the only path that will write "CR is gone" to the database. It previously combined N times upsert and
    All soft deletions are squeezed into one transaction, so if there is a problem with any CR, the entire batch will be rolled back - even soft deletions are not possible in this round.
    Didn't run. The caller only prints one sentence to advance to the next round, and /readyz only pings the database, which is invisible. The result is
    Deployments that have long since disappeared from the cluster are permanently deleted_at IS NULL: they are always in the list, always counted, and always
    Blocking the migration of many merchants.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "sites.db"
        self.store = postgres_store(self.path)
        self.store.migrate()

    def seed(self, service: str, *, merchant: str = "merchant-a") -> dict:
        cr = _deployment_cr(merchant, "alice", service)
        self.store.upsert_site_deployment(cr)
        return cr

    def test_one_poison_cr_does_not_freeze_the_whole_reconciliation(self) -> None:
        """Good deeds will be written, bad deeds will be skipped, and soft deletions will still be executed - these three things must be true at the same time."""
        healthy = self.seed("healthy")
        unparsable = self.seed("unparsable")
        rejected = self.seed("rejected")
        self.seed("vanished")

        # A good job needs to have evidence that "it was actually written in this round", and just being "still there" cannot prove that it has not been rolled back.
        healthy["spec"]["image"] = "example.invalid/app:v2"
        # Bad method 1: Python side parsing will not pass (CRD sets merchantID as required).
        unparsable["spec"].pop("merchantID")
        # Bad method two: The Python side can parse it, but the database's CHECK rejects it. Test one strip on each layer - only test
        # In the former case, the statement will report an error and mark the PostgreSQL transaction as aborted. This path is not taken by anyone.
        rejected["spec"]["port"] = 70000
        # vanished is not in this batch - it is the one that says "the CR really disappeared from the cluster".

        result = self.store.sync_snapshot([healthy, unparsable, rejected])

        # ① The good rows are indeed written in, but the entire batch is not rolled back.
        alive = self.store.get_deployment("merchant-a", "alice", "healthy")
        self.assertEqual(alive["image"], "example.invalid/app:v2")
        self.assertIsNone(alive["deleted_at"])

        # ② Neither of the two bad CRs were soft deleted by mistake - they are still alive in the cluster, but they are not synchronized this round.
        for service in ("unparsable", "rejected"):
            row = self.store.get_deployment("merchant-a", "alice", service)
            self.assertIsNone(row["deleted_at"], service)
            self.assertNotEqual(row["phase"], "Deleted", service)

        # ③ The one that really disappeared is still soft-deleted normally. This is also a positive comparison of ②: without it, ② only
        #    If the entire section is soft-deleted, it will turn green without running.
        gone = self.store.get_deployment("merchant-a", "alice", "vanished")
        self.assertIsNotNone(gone["deleted_at"])
        self.assertEqual(gone["phase"], "Deleted")

        # Skip is a silent drift, only the count can see it.
        self.assertEqual(result.synced, 1)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.soft_deleted, 1)
        self.assertTrue(result.reclaimed)

    def test_a_poison_cr_stays_out_of_nobodys_way_on_the_next_round(self) -> None:
        """After the bad CR is fixed, the next round will converge as usual - skipping it does not blacklist it."""
        poisoned = self.seed("flaky")
        broken = json.loads(json.dumps(poisoned))
        broken["spec"]["port"] = 70000
        self.assertEqual(self.store.sync_snapshot([broken]).skipped, 1)

        poisoned["spec"]["image"] = "example.invalid/app:v3"
        result = self.store.sync_snapshot([poisoned])
        self.assertEqual((result.synced, result.skipped), (1, 0))
        self.assertEqual(
            self.store.get_deployment("merchant-a", "alice", "flaky")["image"],
            "example.invalid/app:v3",
        )

    def test_an_unnamed_item_holds_back_reclamation_for_that_round(self) -> None:
        """Entries whose names cannot even be read -> Not recycled this round, instead of guessing.

        Soft deletion is located by cr_name. An unrecognizable entry means that any line could be it, so there's no way
        Prove that a certain row "does not exist in the cluster" - it is better to make the snapshot an older round than to take the still alive
        The deployment is marked as deleted.
        """
        self.seed("still-there")
        result = self.store.sync_snapshot([{"spec": {}}, {"metadata": None}])

        self.assertFalse(result.reclaimed)
        self.assertEqual(result.soft_deleted, 0)
        self.assertEqual(result.skipped, 2)
        row = self.store.get_deployment("merchant-a", "alice", "still-there")
        self.assertIsNone(row["deleted_at"])

        # Forward comparison: The same record must be soft deleted in a round where all entries are recognized. Otherwise
        # That assertion above may just be because soft deletion of the entire section isn't working at all.
        healthy = self.seed("companion")
        self.assertTrue(self.store.sync_snapshot([healthy]).reclaimed)
        self.assertIsNotNone(
            self.store.get_deployment("merchant-a", "alice", "still-there")[
                "deleted_at"
            ]
        )

    def test_zero_writes_does_not_mean_the_cluster_is_empty(self) -> None:
        """Zero entries written ≠ zero entries in the cluster. Recycling depends on "who saw it", not "who wrote it".

        When the entire batch of CRs fails to be parsed, synced is 0, but those CRs are still in the cluster. take this round
        Treating it as "the cluster is empty" means an accidental deletion of the entire platform. The control group is a truly empty set: again zero entries are written,
        That is the round when all records should be recovered.
        """
        self.seed("survivor")
        broken = self.seed("bystander")
        broken["spec"].pop("merchantID")

        blind = self.store.sync_snapshot([broken])
        self.assertEqual((blind.synced, blind.skipped), (0, 1))
        # If you see it, don't move it, even a single field is not parsed.
        self.assertIsNone(
            self.store.get_deployment(
                "merchant-a", "alice", "bystander"
            )["deleted_at"]
        )
        # The one that did not appear in this batch was recycled as usual - proving that the entire section of the recycling was indeed running.
        self.assertEqual(blind.soft_deleted, 1)
        self.assertIsNotNone(
            self.store.get_deployment(
                "merchant-a", "alice", "survivor"
            )["deleted_at"]
        )

        # Control group: The collection is really empty, and the remaining items should be recycled.
        empty = self.store.sync_snapshot([])
        self.assertEqual(
            (empty.synced, empty.skipped, empty.soft_deleted, empty.reclaimed),
            (0, 0, 1, True),
        )
        self.assertIsNotNone(
            self.store.get_deployment(
                "merchant-a", "alice", "bystander"
            )["deleted_at"]
        )


class _AbortingCursor:
    """Record SQL and fail the first upsert to model PostgreSQL abort semantics.

    PostgreSQL rejects every later statement in a failed transaction. Snapshot
    fault tolerance therefore depends on wrapping each row in its own savepoint.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self._rows: list[tuple] = []
        self._failed_once = False

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())
        self.statements.append(text)
        self._rows = []
        if text.startswith("SELECT cr_name"):
            self._rows = [("ghost",)]
        elif text.startswith("INSERT INTO sites_deployments") and not self._failed_once:
            self._failed_once = True
            raise RuntimeError("duplicate key value violates unique constraint")

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self, cursor: _AbortingCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def cursor(self) -> _AbortingCursor:
        return self._cursor

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class SnapshotPostgresDialectTests(unittest.TestCase):
    """Line-by-line fault tolerance in PostgreSQL relies on savepoint, where the statements it issues are pinned."""

    def _run(self) -> tuple[_AbortingCursor, object]:
        cursor = _AbortingCursor()
        store = Store(_POSTGRES, lambda: _FakeConnection(cursor))
        result = store.sync_snapshot(
            [
                _deployment_cr("merchant-a", "alice", "first"),
                _deployment_cr("merchant-a", "alice", "second"),
            ]
        )
        return cursor, result

    def test_a_failed_write_is_rolled_back_to_its_own_savepoint(self) -> None:
        cursor, result = self._run()
        inserts = [
            index for index, sql in enumerate(cursor.statements)
            if sql.startswith("INSERT INTO sites_deployments")
        ]
        # After the first error is reported, the second one can still be sent - without the savepoint, PostgreSQL will
        # It rejects everything else from this round along with it.
        self.assertEqual(len(inserts), 2, cursor.statements)
        between = cursor.statements[inserts[0] + 1:inserts[1]]
        self.assertIn("ROLLBACK TO SAVEPOINT sites_sync_item", between)
        self.assertEqual((result.synced, result.skipped), (1, 1))

    def test_every_savepoint_is_released(self) -> None:
        cursor, _ = self._run()
        opened = cursor.statements.count("SAVEPOINT sites_sync_item")
        released = cursor.statements.count("RELEASE SAVEPOINT sites_sync_item")
        self.assertEqual(opened, 2)
        # If you do not release them, you will pile savepoints into a long transaction, and pile up a batch of snapshots for each round.
        self.assertEqual(released, opened)

    def test_reclamation_still_runs_after_a_failed_write(self) -> None:
        cursor, result = self._run()
        joined = "\n".join(cursor.statements)
        self.assertIn("SELECT cr_name", joined)
        self.assertIn("UPDATE sites_deployments SET phase = 'Deleted'", joined)
        self.assertEqual(result.soft_deleted, 1)
        # Dialect rendering must use PostgreSQL placeholders all the way through.
        self.assertNotIn("?", joined)
        self.assertIn("WHERE cr_name = %s", joined)


class DeletedStatusTests(unittest.TestCase):
    """set_status('Deleted') must also drop deleted_at.

    The API uses this to mark the record as Deleted when Kubernetes returns a 404. And all the "are you still alive?"
    The criteria all look at deleted_at IS NULL - without this, the deployment that has not been in the cluster for a long time will be forever
    Stay in the active set: Usage is inflated, multi-merchant migration is blocked by "deleting these active deployments through the API first"
    outside the door, and those CRs are no longer in the cluster at all.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "sites.db"
        self.store = postgres_store(self.path)
        self.store.migrate()
        self.store.upsert_site_deployment(
            _deployment_cr("merchant-a", "alice", "site-a")
        )

    def _backdate_deleted_at(self, stamp: str) -> None:
        """Change the deletion time to a value that is instantly recognizable.

        Idempotence assertions use a distinctive historical timestamp. Wall-clock
        equality rounded to seconds could pass even if COALESCE were missing.
        """
        with postgres_connection(self.path) as connection:
            connection.execute(
                "UPDATE sites_deployments SET deleted_at = %s WHERE service_name = %s",
                (stamp, "site-a"),
            )

    def test_marking_deleted_takes_the_row_out_of_every_active_view(self) -> None:
        # Forward comparison: It is present in every active criterion before marking.
        self.assertEqual(
            [row["service_name"] for row in
             self.store.list_deployments("merchant-a", "alice")],
            ["site-a"],
        )
        self.assertEqual(
            self.store.count_deployments_by_merchant(), {"merchant-a": 1}
        )
        self.assertIsNone(
            self.store.get_deployment("merchant-a", "alice", "site-a")["deleted_at"]
        )

        self.store.set_status(
            "merchant-a", "alice", "site-a",
            "Deleted", "SiteDeployment no longer exists",
        )

        row = self.store.get_deployment("merchant-a", "alice", "site-a")
        self.assertEqual(row["phase"], "Deleted")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(self.store.list_deployments("merchant-a", "alice"), [])
        self.assertEqual(self.store.count_deployments_by_merchant(), {})
        self.assertEqual(self.store.list_all_deployments(), [])

    def test_marking_deleted_twice_keeps_the_first_deletion_moment(self) -> None:
        self.store.set_status(
            "merchant-a", "alice", "site-a", "Deleted", "gone"
        )
        self._backdate_deleted_at("2001-02-03 04:05:06")

        self.store.set_status(
            "merchant-a", "alice", "site-a", "Deleted", "still gone"
        )
        row = self.store.get_deployment("merchant-a", "alice", "site-a")
        self.assertEqual(
            row["deleted_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "2001-02-03 04:05:06",
        )
        # The other fields are updated as usual - idempotent is the deletion time, not the entire row.
        self.assertEqual(row["message"], "still gone")

    def test_any_other_phase_leaves_the_deletion_moment_alone(self) -> None:
        """Only Deleted contains deleted_at, and other states cannot be touched."""
        for phase in ("Ready", "Failed", "Deleting"):
            self.store.set_status(
                "merchant-a", "alice", "site-a", phase, phase.lower()
            )
            row = self.store.get_deployment("merchant-a", "alice", "site-a")
            # Forward comparison: First prove that this UPDATE indeed hits this row. Without these two sentences, once
            # set_status does not take effect at all (for example, WHERE never matches), "deleted_at still
            # "None" will still be green - that means not executing it as executing it.
            self.assertEqual(row["phase"], phase)
            self.assertEqual(row["message"], phase.lower())
            self.assertIsNone(row["deleted_at"], phase)

        # Even if a deleted row is updated by other statuses, the deletion time cannot be erased - that is equivalent to losing a record.
        # Quietly revive back to active collection from "Deleted".
        self.store.set_status("merchant-a", "alice", "site-a", "Deleted", "gone")
        self._backdate_deleted_at("2001-02-03 04:05:06")
        self.store.set_status("merchant-a", "alice", "site-a", "Failed", "later")
        deleted_at = self.store.get_deployment(
            "merchant-a", "alice", "site-a"
        )["deleted_at"]
        self.assertEqual(
            deleted_at.strftime("%Y-%m-%d %H:%M:%S"),
            "2001-02-03 04:05:06",
        )

    def test_the_deleted_marker_cannot_cross_a_merchant_boundary(self) -> None:
        self.store.create_merchant(
            "merchant-b", "Merchant B", token_digest("key-merchant-b"), 10, 20
        )
        self.store.upsert_site_deployment(
            _deployment_cr("merchant-b", "alice", "site-a")
        )
        self.store.set_status(
            "merchant-b", "alice", "site-a", "Deleted", "gone"
        )
        self.assertIsNotNone(
            self.store.get_deployment(
                "merchant-b", "alice", "site-a"
            )["deleted_at"]
        )
        # The same tenant, the same service, the line under another merchant's name must remain unscathed.
        self.assertIsNone(
            self.store.get_deployment(
                "merchant-a", "alice", "site-a"
            )["deleted_at"]
        )


# ---------------------------------------------------------------------------
# Exception translation: "Already exists" leaves only unique constraint violations. Used create_tenant / create_merchant
# Translate **any** exception into already exists, and a database failure will be reported as a 409 type conflict by the API.
# The troubleshooter is led to look for a race condition that doesn't exist.
# ---------------------------------------------------------------------------

class _ViolatingCursor:
    """Throws the given exception on INSERT of tenants/merchants, and the rest of the statements quiet the cursor."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())
        if text.startswith(
            ("INSERT INTO sites_tenants", "INSERT INTO sites_merchants")
        ):
            raise self._exc

    def fetchone(self):
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def close(self) -> None:
        pass


def _pg_error(sqlstate: str) -> RuntimeError:
    """The duck with the unusual look of psycopg: psycopg is an optional dependency, and tests cannot rely on its existence."""
    error = RuntimeError(f"db error: {sqlstate}")
    error.diag = types.SimpleNamespace(sqlstate=sqlstate)
    return error


def _refuse_to_connect(*_args, **_kwargs):
    raise OSError("connection refused")


class CreateConflictTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = postgres_store(Path(directory.name) / "sites.db")
        self.store.migrate()
        self.store.create_merchant(
            "acme", "Acme", token_digest("key-acme"), 10, 20
        )
        self.store.create_tenant(
            "acme", "alice", token_digest("token-alice"),
            max_deployments=5, max_public_routes=1,
        )

    def test_duplicates_still_read_as_conflicts_on_postgres(self) -> None:
        with self.assertRaises(StorageError) as caught:
            self.store.create_tenant(
                "acme", "alice", token_digest("token-fresh"),
                max_deployments=5, max_public_routes=1,
            )
        self.assertIn("already exists", str(caught.exception))

        with self.assertRaises(StorageError) as caught:
            self.store.create_merchant(
                "acme", "Fresh", token_digest("key-fresh"), 1, 1
            )
        self.assertIn("already exists", str(caught.exception))

    def test_a_postgres_unique_violation_still_reads_as_a_conflict(self) -> None:
        cursor = _ViolatingCursor(_pg_error("23505"))
        store = Store(_POSTGRES, lambda: _FakeConnection(cursor))
        with self.assertRaises(StorageError) as caught:
            store.create_tenant(
                "acme", "alice", "digest",
                max_deployments=1, max_public_routes=1,
            )
        self.assertIn("already exists", str(caught.exception))

    def test_other_postgres_sqlstates_are_not_conflicts(self) -> None:
        # The basis for judgment is SQLSTATE instead of "even with diag": 53300 (the number of connections exceeds the limit) or the like
        # Server-side errors must go through a general failure, otherwise half of the repairs will mean that they have not been repaired.
        cursor = _ViolatingCursor(_pg_error("53300"))
        store = Store(_POSTGRES, lambda: _FakeConnection(cursor))
        with self.assertRaises(StorageError) as caught:
            store.create_merchant("acme", "Acme", "digest", 1, 1)
        self.assertNotIn("already exists", str(caught.exception))

    def test_an_unreachable_database_is_not_a_conflict(self) -> None:
        # Failure to connect to the library (driver unreachable exception) is 503 material, not 409 material. Both dialects are required
        # Nail: Distortion translation used to be dialect-independent.
        for dialect in (_POSTGRES,):
            store = Store(dialect, _refuse_to_connect)
            with self.assertRaises(StorageError) as caught:
                store.create_tenant(
                    "acme", "alice", "digest",
                    max_deployments=1, max_public_routes=1,
                )
            self.assertNotIn("already exists", str(caught.exception), dialect)

            store = Store(dialect, _refuse_to_connect)
            with self.assertRaises(StorageError) as caught:
                store.create_merchant("acme", "Acme", "digest", 1, 1)
            self.assertNotIn("already exists", str(caught.exception), dialect)


# ---------------------------------------------------------------------------
# PostgreSQL connection reuse: one cached connection per thread, which is explored before lending and discarded if an error occurs.
# The pile only records behavior (how many times it is opened, how many times it is closed, and what is posted), and is not connected to the real database.
# ---------------------------------------------------------------------------

class _PoolingCursor:
    def __init__(self, state: dict) -> None:
        self._state = state

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())
        # "SELECT 1" is the detection statement of Store, which is only managed by break_ping - otherwise it will be simulated
        # When the "business statement explodes", the exploration activity will also be exploded, and the path of business failure will not be tested.
        if text == "SELECT 1":
            if self._state["break_ping"]:
                raise RuntimeError("server closed the connection unexpectedly")
        elif text.startswith("SELECT") and self._state["break_reads"]:
            raise RuntimeError("terminating connection due to crash")
        self._state["statements"].append(text)

    def fetchone(self):
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def close(self) -> None:
        pass


class _PoolingConnection:
    def __init__(self, state: dict) -> None:
        self._state = state
        state["opened"] += 1

    def __enter__(self) -> "_PoolingConnection":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def cursor(self) -> _PoolingCursor:
        return _PoolingCursor(self._state)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self._state["closed"] += 1


class PostgresConnectionReuseTests(unittest.TestCase):
    def _store(self) -> tuple[Store, dict]:
        state = {
            "opened": 0, "closed": 0,
            "statements": [],
            "break_ping": False, "break_reads": False,
        }
        return Store(_POSTGRES, lambda: _PoolingConnection(state)), state

    def test_operations_on_one_thread_share_one_connection(self) -> None:
        store, state = self._store()
        store.list_tenants()
        store.list_tenants()
        # Two operations and one handshake: there is only one probe statement before the second loan, and there is no reconnect.
        self.assertEqual(state["opened"], 1)
        self.assertEqual(state["closed"], 0)
        self.assertEqual(state["statements"].count("SELECT 1"), 1)

    def test_a_dead_cached_connection_is_replaced(self) -> None:
        store, state = self._store()
        store.list_tenants()

        # The connection was disconnected by the server between two operations: I discovered that the old connection was closed and replaced with a new one.
        state["break_ping"] = True
        store.list_tenants()
        self.assertEqual(state["opened"], 2)
        self.assertEqual(state["closed"], 1)

        # The replaced one is reused as usual and no more handshakes are required.
        state["break_ping"] = False
        store.list_tenants()
        self.assertEqual(state["opened"], 2)

    def test_a_failed_statement_discards_the_connection(self) -> None:
        store, state = self._store()
        store.list_tenants()

        # The business statement exploded: `with connection` transaction was rolled back, and the cache connection was also discarded.
        state["break_reads"] = True
        with self.assertRaises(StorageError):
            store.list_tenants()
        self.assertEqual(state["closed"], 1)

        state["break_reads"] = False
        store.list_tenants()
        self.assertEqual(state["opened"], 2)


# ---------------------------------------------------------------------------
# PostgreSQL must not use a process-global storage lock; independent database
# operations need to overlap across request threads.
# ---------------------------------------------------------------------------

class LockScopeTests(unittest.TestCase):
    def test_postgres_statement_groups_overlap_instead_of_queueing(self) -> None:
        # The barrier is placed inside the transaction body: only when the two operations really proceed to the statement layer in parallel,
        # Only then can the two sides meet. If the PG path still passes the global lock, the later arrival will be blocked by the lock, and the first arrival will wait until it is full.
        # Timeout - barrier explodes, test red.
        barrier = threading.Barrier(2)

        class _Cursor:
            def execute(self, sql: str, params: tuple = ()) -> None:
                barrier.wait(timeout=5)

            def fetchall(self) -> list[tuple]:
                return []

            def close(self) -> None:
                pass

        class _Connection:
            def __enter__(self) -> "_Connection":
                return self

            def __exit__(self, *exc) -> bool:
                return False

            def cursor(self) -> _Cursor:
                return _Cursor()

            def commit(self) -> None:
                pass

            def close(self) -> None:
                pass

        store = Store(_POSTGRES, _Connection)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                store.list_tenants()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))


# ---------------------------------------------------------------------------
# Single transaction of merchant PATCH: Quota and display name are combined into one UPDATE. Once they were two independent
# There are two transactions in the statement, and if the document fails in the middle, it will leave a partial update.
# ---------------------------------------------------------------------------

class MerchantPatchTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "sites.db"
        self.store = postgres_store(self.path)
        self.store.migrate()
        self.store.create_merchant(
            "acme", "Acme", token_digest("key-acme"), 10, 20
        )

    def test_quota_and_name_land_together(self) -> None:
        self.store.update_merchant(
            "acme", display_name="Acme Ltd", max_tenants=42
        )
        row = self.store.merchant("acme")
        self.assertEqual(row["display_name"], "Acme Ltd")
        self.assertEqual(row["max_tenants"], 42)
        # The item that is not given does not move - the merge entry also has PATCH semantics, not PUT.
        self.assertEqual(row["max_deployments"], 20)

    def test_the_single_field_entries_keep_their_contract(self) -> None:
        # The two old entry points have unchanged signatures and unchanged behavior: This is a guardrail for existing call points in the API layer.
        self.store.update_merchant_quota("acme", max_tenants=7)
        self.assertEqual(self.store.merchant("acme")["display_name"], "Acme")
        self.store.update_merchant_display_name("acme", "Renamed")
        row = self.store.merchant("acme")
        self.assertEqual(row["display_name"], "Renamed")
        self.assertEqual(row["max_tenants"], 7)
        self.assertEqual(row["max_deployments"], 20)
        # An empty PATCH cannot silently succeed: the caller returns 400 accordingly.
        with self.assertRaises(ValueError):
            self.store.update_merchant("acme")

    def test_a_merged_patch_is_one_update_statement(self) -> None:
        cursor = _RecordingCursor({})
        store = Store(_POSTGRES, lambda: _FakeConnection(cursor))
        store.update_merchant(
            "acme", display_name="Acme Ltd", max_tenants=42, max_deployments=99
        )
        updates = [
            sql
            for sql, _ in cursor.statements
            if sql.startswith("UPDATE sites_merchants")
        ]
        # An UPDATE has all three columns: a single statement is naturally atomic, and a partial update has no landing point.
        self.assertEqual(len(updates), 1, cursor.statements)
        for column in ("display_name", "max_tenants", "max_deployments"):
            self.assertIn(f"{column} = %s", updates[0])

    def test_the_single_field_entries_still_emit_one_statement_each(self) -> None:
        cursor = _RecordingCursor({})
        store = Store(_POSTGRES, lambda: _FakeConnection(cursor))
        store.update_merchant_quota("acme", max_tenants=7)
        store.update_merchant_display_name("acme", "Renamed")
        updates = [
            sql
            for sql, _ in cursor.statements
            if sql.startswith("UPDATE sites_merchants")
        ]
        self.assertEqual(len(updates), 2, cursor.statements)
        self.assertIn("max_tenants = %s", updates[0])
        self.assertNotIn("display_name = %s", updates[0])
        self.assertIn("display_name = %s", updates[1])

    def test_a_failed_merged_patch_leaves_nothing_behind(self) -> None:
        before = self.store.merchant("acme")

        class FailingCursor(_RecordingCursor):
            def execute(self, sql: str, params: tuple = ()) -> None:
                super().execute(sql, params)
                if "UPDATE sites_merchants" in sql:
                    raise RuntimeError("injected PostgreSQL write failure")

        cursor = FailingCursor({})
        bombed = Store(_POSTGRES, lambda: _FakeConnection(cursor))
        with self.assertRaises(StorageError):
            bombed.update_merchant("acme", display_name="Ghost", max_tenants=99)
        # Zero residue: Names and quotas remain at their original values. The old two consecutive adjustments will now leave "Quota changed,
        # The partial update whose name has not been changed - that is exactly the form that is to be eliminated by combining it into one sentence.
        self.assertEqual(self.store.merchant("acme"), before)


if __name__ == "__main__":
    unittest.main()
