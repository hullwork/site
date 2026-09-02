"""Registry proxy authorization: read, write, and enumeration boundaries.

The static layer verifies wiring without external tools. The integration layer, enabled
with ``SITES_TEST_REGISTRY_PROXY=1``, starts registry and proxy containers to prove
behavior that configuration inspection cannot—especially that cluster-side ``/v2/`` must
return 401 so Docker and BuildKit negotiate credentials rather than treating the registry
as unauthenticated.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import unittest
import urllib.error
import urllib.request

from tests import chart

from sites.validation import STATIC_IMAGE


ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = "07-build-plane.yaml"
CONTROL = "10-control-plane.yaml"


def load(name: str) -> list[dict]:
    return chart.documents(name)


def pick(docs: list[dict], kind: str, name: str) -> dict:
    for doc in docs:
        if doc["kind"] == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"{kind}/{name} Not in the list")


class ProxyWiringTests(unittest.TestCase):
    """Wiring shape: If any one of them is damaged, proxying will silently fail."""

    def setUp(self) -> None:
        self.docs = load(MANIFEST)
        self.deploy = pick(self.docs, "Deployment", "sites-registry")
        self.containers = {
            c["name"]: c for c in self.deploy["spec"]["template"]["spec"]["containers"]
        }
        self.conf = pick(self.docs, "ConfigMap", "sites-registry-proxy")["data"]["nginx.conf"]

    def test_registry_itself_listens_only_on_loopback(self) -> None:
        """The registry entity must be on loopback only.

        This is the premise of the entire set of proxying: after changing it back to 0.0.0.0, anything that can reach the Pod IP can be directly connected
        registry port, the proxy is bypassed along with its authentication - and everything works as usual, there is no
        Any errors reported should be directed here.
        """
        env = {e["name"]: e["value"] for e in self.containers["registry"]["env"]}
        for key in ("REGISTRY_HTTP_ADDR", "REGISTRY_HTTP_DEBUG_ADDR"):
            self.assertTrue(
                env.get(key, "").startswith("127.0.0.1:"),
                f"{key} Must be tied to 127.0.0.1, actual {env.get(key)!r}",
            )
        self.assertNotIn(
            "ports",
            self.containers["registry"],
            "The registry container should no longer expose its own port. The external side is held by the proxy.",
        )

    def test_only_the_node_facing_port_is_published_on_the_host(self) -> None:
        """hostPort only gives node faces. The appearance of the authentication plane on the node address exposes it to the entire machine."""
        ports = {p["name"]: p for p in self.containers["proxy"]["ports"]}
        self.assertEqual(ports["registry"]["hostPort"], 5000)
        self.assertNotIn(
            "hostPort", ports["authenticated"], "There should be no hostPort on the authentication side."
        )

    def test_cluster_traffic_lands_on_the_authenticated_port(self) -> None:
        """Service must specify the interface. Referring back to the node face = push and enumeration are now anonymous again."""
        svc = pick(self.docs, "Service", "sites-registry")
        ports = {port["name"]: port for port in svc["spec"]["ports"]}
        self.assertEqual(ports["registry"]["targetPort"], "authenticated")
        self.assertEqual(ports["pull"]["port"], 5001)
        self.assertEqual(ports["pull"]["targetPort"], "registry")

    def test_registry_policies_allow_the_service_target_port(self) -> None:
        """NetworkPolicy matches the Pod port after the Service DNAT, not the Service port."""
        svc = pick(self.docs, "Service", "sites-registry")
        target_name = next(
            port["targetPort"]
            for port in svc["spec"]["ports"]
            if port["name"] == "registry"
        )
        container_ports = {
            port["name"]: port["containerPort"]
            for port in self.containers["proxy"]["ports"]
        }
        target_port = container_ports[target_name]
        for name, direction in (
            ("sites-registry", "ingress"),
            ("sites-builder", "egress"),
        ):
            policy = pick(self.docs, "NetworkPolicy", name)
            allowed = {
                port["port"]
                for rule in policy["spec"][direction]
                for port in rule.get("ports", [])
            }
            self.assertIn(target_port, allowed, name)

        registry_policy = pick(self.docs, "NetworkPolicy", "sites-registry")
        node_rules = [
            rule
            for rule in registry_policy["spec"]["ingress"]
            if any("ipBlock" in source for source in rule.get("from", []))
        ]
        self.assertEqual(len(node_rules), 1)
        self.assertEqual(node_rules[0]["ports"], [{"protocol": "TCP", "port": 5000}])

    def test_node_face_refuses_everything_except_reads(self) -> None:
        node_face = self.conf.split("listen 5000;")[1].split("listen 5002;")[0]
        self.assertIn("limit_except GET HEAD", node_face, "Write methods must be blocked")
        self.assertIn("deny all", node_face, "The enumeration side (catalog/tags) must be blocked")
        self.assertNotIn("auth_basic", node_face, "The node does not accept authentication and should not issue a challenge.")

    def test_cluster_face_authenticates_everything_including_the_ping(self) -> None:
        """There cannot be any anonymous exceptions on the cluster side, especially not `/v2/`.

        Allowing ping will cause the client to determine this registry as "no authentication required", and then the credentials will be
        It will not be sent - the performance is that push fails with 401, and the credentials are obviously correct.
        """
        cluster_face = self.conf.split("listen 5002;")[1]
        self.assertIn("auth_basic_user_file", cluster_face)
        self.assertNotIn(
            "location = /v2/",
            cluster_face,
            "Opening anonymous exceptions for /v2/ on the cluster side will destroy the client's authentication negotiation.",
        )

    def test_builder_gets_credentials_the_tenant_cannot_read(self) -> None:
        """The credentials are hung on the Job container and buildctl needs to know where to read them."""
        source = (ROOT / "src/sites/builds.py").read_text(encoding="utf-8")
        self.assertIn("DOCKER_CONFIG", source, "If you don't set it, you have to rely on $HOME to infer it. If you change user, it will become invalid.")
        self.assertIn("REGISTRY_AUTH_SECRET", source)

    def test_control_plane_reads_the_password_from_a_file(self) -> None:
        """The password cannot be set as an environment variable: it will be entered into kubectl describe and will also be printed out by the env of the same Pod."""
        for name in ("sites-api", "sites-operator"):
            deploy = pick(load(CONTROL), "Deployment", name)
            container = deploy["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            self.assertEqual(env.get("SITES_REGISTRY_USERNAME"), "sites", name)
            self.assertNotIn("SITES_REGISTRY_PASSWORD", env, f"{name} Passwords should not be placed in environment variables")
            mounts = {m["name"] for m in container["volumeMounts"]}
            self.assertIn("registry-auth", mounts, name)

    def test_static_runtime_pin_matches_the_registry_sidecar(self) -> None:
        """One nginx build serves static sites and fronts the registry.

        Compared by repository and digest rather than by the whole reference:
        STATIC_IMAGE spells ``repository:tag@digest`` while the chart's
        site.image helper drops the tag once a digest is set.  The retired
        manifests/ copy carried the code's spelling verbatim, so a substring
        match passed there and would fail here for a formatting difference
        while the two still name the same bytes.
        """
        def repository_and_digest(reference: str) -> tuple[str, str]:
            repository, _, digest = reference.partition("@")
            self.assertTrue(digest.startswith("sha256:"), reference)
            return repository.split(":")[0], digest

        rendered = {
            container["image"]
            for deployment in load(MANIFEST)
            if deployment["kind"] == "Deployment"
            for container in deployment["spec"]["template"]["spec"]["containers"]
        }
        self.assertIn(
            repository_and_digest(STATIC_IMAGE),
            {repository_and_digest(image) for image in rendered},
            f"no registry container runs the static runtime image; rendered {rendered}",
        )


@unittest.skipUnless(
    os.getenv("SITES_TEST_REGISTRY_PROXY") and shutil.which("docker"),
    "Requires docker, and SITES_TEST_REGISTRY_PROXY=1 is explicitly turned on",
)
class ProxyBehaviourTests(unittest.TestCase):
    """Really start the registry + agent and run the rule matrix.

    Use `--network container:` to share the network namespace to accurately simulate loopback in the same Pod.
    """

    NODE = "http://127.0.0.1:15999"
    CTRL = "http://127.0.0.1:15998"
    REPO = "/v2/local/acme/alice/web"
    REGISTRY = (
        "registry:3.1.1@sha256:"
        "1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33"
    )
    PROXY = (
        "nginxinc/nginx-unprivileged:1.29-alpine@sha256:"
        "0c79d56aee561a1d81c63f00eee5fb5fe29279560cdc55e91425133104c7fbe6"
    )

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sites-registry-proxy-"))
        cls.password = os.urandom(16).hex()
        conf = pick(load(MANIFEST), "ConfigMap", "sites-registry-proxy")["data"]["nginx.conf"]
        (cls.tmp / "nginx.conf").write_text(conf)
        crypt = subprocess.run(
            ["openssl", "passwd", "-apr1", cls.password],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (cls.tmp / "htpasswd").write_text(f"sites:{crypt}\n")
        cls._down()
        subprocess.run(
            ["docker", "run", "-d", "--name", "sites-regproxy-registry",
             "-p", "15999:5000", "-p", "15998:5002",
             "-e", "REGISTRY_HTTP_ADDR=127.0.0.1:5010",
             "-e", "REGISTRY_HTTP_DEBUG_ADDR=127.0.0.1:5011",
             "-e", "REGISTRY_STORAGE_DELETE_ENABLED=true", cls.REGISTRY],
            check=True, capture_output=True, timeout=180,
        )
        subprocess.run(
            ["docker", "run", "-d", "--name", "sites-regproxy-proxy",
             "--network", "container:sites-regproxy-registry",
             "-v", f"{cls.tmp}/nginx.conf:/etc/nginx/nginx.conf:ro",
             "-v", f"{cls.tmp}/htpasswd:/etc/nginx/auth/htpasswd:ro",
             "--user", "1000:1000", "--read-only", "--tmpfs", "/tmp", cls.PROXY],
            check=True, capture_output=True, timeout=180,
        )
        cls._push_fixture()

    @classmethod
    def _down(cls) -> None:
        subprocess.run(
            ["docker", "rm", "-f", "sites-regproxy-registry", "sites-regproxy-proxy"],
            capture_output=True, timeout=120,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._down()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _push_fixture(cls) -> None:
        """Push a minimal image. Each rebuild: the last use case will be deleted, and the second run will be 404 without restarting."""
        import time

        cfg = cls.tmp / "dockercfg"
        cfg.mkdir(exist_ok=True)
        auth = base64.b64encode(f"sites:{cls.password}".encode()).decode()
        (cfg / "config.json").write_text(json.dumps(
            {"auths": {"127.0.0.1:15998": {"auth": auth}}}
        ))
        (cls.tmp / "Dockerfile").write_text("FROM scratch\nCOPY nginx.conf /x\n")
        image = "127.0.0.1:15998/local/acme/alice/web:v1"
        for _ in range(30):                      # Wait for nginx and registry to get up
            try:
                urllib.request.urlopen(cls.NODE + "/v2/", timeout=2)
                break
            except Exception:                    # noqa: BLE001
                time.sleep(1)
        subprocess.run(["docker", "build", "-q", "-t", image, str(cls.tmp)],
                       check=True, capture_output=True, timeout=180)
        subprocess.run(["docker", "--config", str(cfg), "push", image],
                       check=True, capture_output=True, timeout=300)

    def setUp(self) -> None:
        """The fixture heals itself.

        The two use cases will delete the manifest, and unittest will be executed in alphabetical order of method names——
        test_cluster_face_serves… (the last step is DELETE) comes before test_node_face…,
        As a result, the latter could not pull anything, and reported 404 instead of anything related to proxying. between tests
        The execution sequence transfer status is the most difficult type of false red to check.
        """
        status, _ = self.call(self.CTRL, "HEAD", f"{self.REPO}/manifests/v1", auth=True)
        if status != 200:
            self._push_fixture()

    def call(self, base: str, method: str, path: str, auth: bool = False):
        req = urllib.request.Request(base + path, method=method)
        req.add_header("Accept", "application/vnd.oci.image.manifest.v1+json,"
                                 "application/vnd.docker.distribution.manifest.v2+json")
        if auth:
            token = base64.b64encode(f"sites:{self.password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.headers.get("Docker-Content-Digest", "")
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    def digest(self) -> str:
        _, value = self.call(self.CTRL, "HEAD", f"{self.REPO}/manifests/v1", auth=True)
        self.assertTrue(value, "The digest cannot be obtained and the clamp is not pushed up.")
        return value

    def test_node_face_serves_pulls_and_refuses_the_rest(self) -> None:
        for method, path, want, why in [
            ("GET", "/v2/", 200, "containerd ping, if blocked, the anonymous pull will be completely useless"),
            ("GET", f"{self.REPO}/manifests/v1", 200, "Pull manifest"),
            ("HEAD", f"{self.REPO}/manifests/v1", 200, "Pull manifest"),
            ("GET", "/v2/_catalog", 403, "Full platform merchant/tenant/service enumeration"),
            ("GET", f"{self.REPO}/tags/list", 403, "Single repo version enumeration"),
            ("POST", f"{self.REPO}/blobs/uploads/", 403, "Open an upload session"),
            ("PUT", f"{self.REPO}/manifests/v2", 403, "Cover other tags"),
            ("GET", "/v2/x/manifests/../../_catalog", 403, "path crossing"),
        ]:
            with self.subTest(method=method, path=path):
                got, _ = self.call(self.NODE, method, path)
                self.assertEqual(got, want, why)
        got, _ = self.call(self.NODE, "DELETE", f"{self.REPO}/manifests/{self.digest()}")
        self.assertEqual(got, 403, "Delete other manifests")

    def test_cluster_face_rejects_anonymous_including_the_ping(self) -> None:
        for path in ["/v2/", "/v2/_catalog", f"{self.REPO}/manifests/v1"]:
            with self.subTest(path=path):
                got, _ = self.call(self.CTRL, "GET", path)
                self.assertEqual(got, 401, "The cluster interface does not accept anonymity, nor does ping.")

    def test_cluster_face_serves_the_control_plane_with_credentials(self) -> None:
        for path in ["/v2/", "/v2/_catalog", f"{self.REPO}/tags/list"]:
            with self.subTest(path=path):
                got, _ = self.call(self.CTRL, "GET", path, auth=True)
                self.assertEqual(got, 200)
        got, _ = self.call(
            self.CTRL, "DELETE", f"{self.REPO}/manifests/{self.digest()}", auth=True
        )
        self.assertEqual(got, 202, "The control plane must be able to recycle build artifacts")

    def test_a_known_repo_is_still_pullable_anonymously(self) -> None:
        """Known residues are written as use cases rather than just comments.

        What proxying blocks is batch enumeration and writing and deletion, not "the image contents between tenants are not visible to each other." Guess the repo
        The name is still available. When it is changed to global authentication + node configuration credentials, this article will be red to remind you to update.
        """
        got, _ = self.call(self.NODE, "GET", f"{self.REPO}/manifests/v1")
        self.assertEqual(got, 200)


if __name__ == "__main__":
    unittest.main()
