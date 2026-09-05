from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "site"

# clusterNetwork.podCIDR has no default in the chart: it is a fact about the
# target cluster and a wrong value silently disables tenant isolation, so every
# `helm template` has to supply one. These calls render the chart directly
# rather than through tests/chart.py, so they state it themselves.
POD_CIDR = ("--set-string", "clusterNetwork.podCIDR=10.201.0.0/16")


class HelmPackageContractTests(unittest.TestCase):
    def test_quickstart_is_a_repository_owned_kubeadm_workflow(self) -> None:
        script = (ROOT / "scripts" / "quickstart-kubeadm.sh").read_text(encoding="utf-8")
        self.assertIn("sudo kubeadm init", script)
        self.assertIn("sudo kubeadm token create", script)
        self.assertIn('getent hosts "$instance"', script)
        self.assertIn('limactl network create "$network" --mode=user-v2', script)
        self.assertIn('--network="lima:$network"', script)
        self.assertIn("node-role.kubernetes.io/worker=worker", script)
        self.assertIn("control-plane node must retain its NoSchedule taint", script)
        self.assertNotIn('node-role.kubernetes.io/control-plane-', script)
        self.assertIn("dev/kubeadm/lima.yaml", script)

    def test_quickstart_lima_template_needs_no_precreated_network(self) -> None:
        template = yaml.safe_load(
            (ROOT / "dev" / "kubeadm" / "lima.yaml").read_text(encoding="utf-8")
        )
        self.assertNotIn("networks", template)
        self.assertEqual(template["cpus"], 4)
        self.assertEqual(template["memory"], "4GiB")
        self.assertEqual(template["disk"], "30GiB")
        self.assertEqual(template["portForwards"][0]["guestPort"], 6443)
        forwards = {
            item["guestPort"]: item["hostPort"]
            for item in template["portForwards"]
        }
        self.assertEqual(forwards[30080], 18090)
        self.assertEqual(forwards[30088], 18098)
        self.assertNotIn(30081, forwards)

        rendered = subprocess.check_output([
            "helm", "template", "site", str(CHART), *POD_CIDR,
            "--set", "localPathProvisioner.enabled=true",
            "--set-string", "localPathProvisioner.allowedNodeNames[0]=site-quickstart-w1",
        ], text=True)
        storage_class = next(
            item for item in yaml.safe_load_all(rendered)
            if isinstance(item, dict) and item.get("kind") == "StorageClass"
        )
        expression = storage_class["allowedTopologies"][0]["matchLabelExpressions"][0]
        self.assertEqual(expression["key"], "kubernetes.io/hostname")
        self.assertEqual(expression["values"], ["site-quickstart-w1"])

    def test_makefile_exposes_the_complete_kubeadm_trial_lifecycle(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        for target in ("quickstart-doctor", "quickstart", "quickstart-scale", "quickstart-status", "quickstart-access", "quickstart-token", "quickstart-clean"):
            self.assertRegex(makefile, rf"(?m)^{target}:")
        script = (ROOT / "scripts" / "quickstart-kubeadm.sh").read_text(encoding="utf-8")
        self.assertIn('open "$url"', script)
        self.assertIn('xdg-open "$url"', script)
        self.assertIn('token) require_command kubectl; show_token ;;', script)
        self.assertIn('"merchantId":"local","userId":"local"', script)
        self.assertIn('--exposure public', script)
        self.assertIn('public URL body digest', script)
        self.assertIn('kube drain "$worker"', script)
        self.assertIn('localPathProvisioner.allowedNodeNames[0]', script)
        self.assertIn('refusing to remove {node}: persistent volume', script)
        self.assertIn('instance_owned "$worker"', script)
        self.assertIn('rollout restart', script)
        self.assertIn('deployment/sites-api deployment/sites-operator deployment/sites-activator', script)
        dependency_wait = script.index('statefulset/sites-postgres')
        restart = script.index('rollout restart')
        self.assertLess(dependency_wait, restart)
        self.assertIn('deployment/sites-registry', script[dependency_wait:restart])
        self.assertIn('deployment/sites-prometheus', script[dependency_wait:restart])
        self.assertIn('--timeout=15m', script[dependency_wait:restart])

    def test_quickstart_explains_every_newcomer_handoff(self) -> None:
        script = (ROOT / "scripts" / "quickstart-kubeadm.sh").read_text(encoding="utf-8")
        self.assertIn("Quickstart doctor passed", script)
        self.assertIn("Docker is installed, but its daemon is not reachable", script)
        self.assertIn("Python 3.12+ is required", script)
        self.assertIn("管理员 token / Admin token", script)
        self.assertIn("进入控制台 / Enter the console", script)
        self.assertIn("without stopping the cluster", script)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for fact in (
            "make quickstart-doctor",
            "8 CPUs, 10 GiB",
            "8–30 minutes",
            "SITES_QUICKSTART_WORKERS=3 make quickstart-scale",
            "not a highly available production topology",
            "permanently deletes",
        ):
            self.assertIn(fact, readme)

    def test_newcomer_docs_name_kubeadm_and_not_the_kind_tool(self) -> None:
        docs = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in ("README.md", "docs/STANDALONE.md")
        )
        self.assertIn("kubeadm", docs)
        self.assertNotRegex(docs, r"(?i)\bkind\b")

    def test_standalone_release_namespace_is_not_redeclared(self) -> None:
        rendered = subprocess.check_output([
            "helm", "template", "site", str(CHART),
            *POD_CIDR,
            "--namespace", "standalone-test",
            "--values", str(CHART / "values-dev.yaml"),
            "--set-string", "namespaces.control=standalone-test",
            "--set-string", "namespaces.gateway=standalone-test",
        ], text=True)
        documents = list(yaml.safe_load_all(rendered))
        namespaces = {
            item["metadata"]["name"] for item in documents
            if isinstance(item, dict) and item.get("kind") == "Namespace"
        }
        self.assertNotIn("standalone-test", namespaces)
        self.assertIn("local-path-storage", namespaces)

    def test_standalone_smoke_uses_a_dynamic_port_and_detects_forward_exit(self) -> None:
        standalone = (ROOT / "scripts" / "standalone.sh").read_text(encoding="utf-8")
        self.assertIn("forward_spec=:8080", standalone)
        self.assertIn('kill -0 "$forward_pid"', standalone)
        self.assertNotIn("SITES_SMOKE_PORT:-18091", standalone)

    def test_chart_has_canonical_independent_release_metadata(self) -> None:
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(chart["name"], "site")
        self.assertEqual(chart["version"], project["project"]["version"])
        self.assertEqual(chart["appVersion"], project["project"]["version"])
        self.assertEqual("MIT", chart["annotations"]["artifacthub.io/license"])
        self.assertEqual("MIT", project["project"]["license"])
        self.assertEqual("MIT License", (ROOT / "LICENSE").read_text().splitlines()[0])
        self.assertFalse((CHART / "package.yaml").exists())

    def test_all_images_accept_digest_overrides(self) -> None:
        schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
        image = schema["$defs"]["image"]
        self.assertIn("digest", image["required"])
        self.assertIn("sha256", image["properties"]["digest"]["pattern"])

        values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
        self.assertEqual("v0.1.0", values["images"]["control"]["tag"])

    def test_default_services_do_not_claim_node_ports(self) -> None:
        values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
        self.assertEqual(values["service"]["type"], "ClusterIP")
        self.assertIsNone(values["service"]["nodePort"])
        self.assertFalse(values["gateway"]["enabled"])

    def test_external_traffic_policy_only_renders_for_external_services(self) -> None:
        def service(service_type: str) -> dict:
            rendered = subprocess.check_output([
                "helm", "template", "site", str(CHART),
                *POD_CIDR,
                "--namespace", "service-test",
                "--set-string", f"service.type={service_type}",
                "--set-string", "namespaces.control=service-test",
                "--set-string", "namespaces.gateway=service-test",
            ], text=True)
            return next(
                item for item in yaml.safe_load_all(rendered)
                if isinstance(item, dict)
                and item.get("kind") == "Service"
                and item.get("metadata", {}).get("name") == "sites-api"
            )

        self.assertNotIn("externalTrafficPolicy", service("ClusterIP")["spec"])
        self.assertEqual("Cluster", service("NodePort")["spec"]["externalTrafficPolicy"])

    def test_chart_declares_existing_secret_names_and_keys_without_rendering_credentials(self) -> None:
        values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
        secrets = values["existingSecrets"]
        self.assertEqual(
            {"api", "database", "registry", "objectStorage"}, set(secrets)
        )
        self.assertEqual("console-session-key", secrets["api"]["consoleSessionKey"])
        self.assertEqual("htpasswd", secrets["registry"]["htpasswdKey"])
        self.assertEqual("password", secrets["database"]["passwordKey"])
        self.assertNotIn("stringData", (CHART / "values-dev.yaml").read_text())
        self.assertNotIn("data:", (CHART / "values-dev.yaml").read_text())

        if shutil.which("helm"):
            rendered = subprocess.run(
                ["helm", "template", "site", str(CHART), *POD_CIDR],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            documents = [item for item in yaml.safe_load_all(rendered) if item]
            self.assertNotIn("Secret", {item.get("kind") for item in documents})
            self.assertIn("secretName: sites-api-token", rendered)
            self.assertIn("key: console-session-key", rendered)

            custom = subprocess.run([
                "helm", "template", "site", str(CHART),
                *POD_CIDR,
                "--set", "existingSecrets.api.name=custom-api",
                "--set", "existingSecrets.api.tokenKey=api-token",
                "--set", "existingSecrets.api.consoleSessionKey=session-key",
                "--set", "existingSecrets.database.name=custom-db",
                "--set", "existingSecrets.database.usernameKey=db-user",
                "--set", "existingSecrets.database.databaseKey=db-name",
                "--set", "existingSecrets.database.passwordKey=db-password",
                "--set", "existingSecrets.registry.name=custom-registry",
                "--set", "existingSecrets.registry.passwordKey=registry-password",
                "--set", "existingSecrets.registry.htpasswdKey=registry-htpasswd",
                "--set", "existingSecrets.objectStorage.name=custom-oss",
                "--set", "existingSecrets.objectStorage.accessKeyIdKey=oss-id",
                "--set", "existingSecrets.objectStorage.accessKeySecretKey=oss-secret",
            ], check=True, capture_output=True, text=True).stdout
            for expected in (
                "secretName: custom-api", "key: session-key", "secretName: custom-db",
                "key: db-password", "secretName: custom-registry",
                "key: registry-htpasswd", "secretName: custom-oss", "key: oss-secret",
            ):
                self.assertIn(expected, custom)

    def test_chart_renders_after_copy_without_sibling_repositories(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        with tempfile.TemporaryDirectory() as directory:
            copied = pathlib.Path(directory) / "chart"
            shutil.copytree(CHART, copied)
            subprocess.run(
                ["helm", "template", "site", str(copied), *POD_CIDR],
                check=True,
                stdout=subprocess.DEVNULL,
            )

    def test_uninstall_drains_finalized_resources_before_removing_operator(self) -> None:
        script = (ROOT / "scripts" / "standalone.sh").read_text(encoding="utf-8")
        uninstall = script[script.index("  uninstall)") :]
        self.assertLess(
            uninstall.index("delete sitedeployments.sites.local"),
            uninstall.index('uninstall "$release"'),
        )
        self.assertLess(
            uninstall.index("delete sitebuilds.sites.local"),
            uninstall.index('uninstall "$release"'),
        )

    def test_release_publishes_oci_chart_and_machine_readable_metadata(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("helm push", workflow)
        self.assertIn("package-metadata.json", workflow)
        self.assertIn("schemaVersion:1", workflow)
        self.assertIn("immutableRef", workflow)
        self.assertIn("runtimeImages", workflow)
        self.assertIn("valuePath", workflow)
        self.assertIn("prepare-release-chart.py prepare", workflow)
        self.assertIn("prepare-release-chart.py verify", workflow)
        self.assertIn("digestPinnedValues", workflow)
        self.assertNotIn("packageDescriptor", workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("staging-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("scanners: vuln,secret", workflow)
        self.assertIn("cosign sign --yes", workflow)
        self.assertIn("trivyGate:true", workflow)
        self.assertIn("cosignSigned:true", workflow)
        self.assertLess(
            workflow.index("Gate staging image with Trivy"),
            workflow.index("Promote scanned digest to release tag"),
        )
        parsed = yaml.load(workflow, Loader=yaml.BaseLoader)
        self.assertNotIn("id-token", parsed["jobs"]["image"]["permissions"])
        self.assertEqual(
            "write", parsed["jobs"]["image-promote"]["permissions"]["id-token"]
        )

    def test_checkout_never_persists_release_credentials(self) -> None:
        for workflow_path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            workflow = workflow_path.read_text(encoding="utf-8")
            checkout_count = workflow.count("uses: actions/checkout@")
            if checkout_count:
                self.assertEqual(
                    checkout_count,
                    workflow.count("persist-credentials: false"),
                    workflow_path,
                )

    def test_release_chart_preparation_pins_every_rendered_control_image(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        digest = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            image_json = temporary / "image-control.json"
            image_json.write_text(json.dumps({
                "repository": "registry.example/site-control",
                "digest": digest,
                "valuePath": "images.control",
            }))
            chart = temporary / "chart"
            pinned_values = temporary / "values.yaml"
            script = ROOT / "scripts" / "prepare-release-chart.py"
            subprocess.run([
                "python3", str(script), "prepare",
                "--chart", str(CHART), "--image-json", str(image_json),
                "--output-chart", str(chart), "--values-output", str(pinned_values),
            ], check=True)
            rendered = temporary / "rendered.yaml"
            rendered.write_text(subprocess.run(
                ["helm", "template", "site", str(chart), *POD_CIDR],
                check=True, capture_output=True, text=True,
            ).stdout)
            subprocess.run([
                "python3", str(script), "verify", "--rendered", str(rendered),
                "--image-json", str(image_json),
            ], check=True)
            self.assertEqual(
                digest,
                yaml.safe_load((chart / "values.yaml").read_text())["images"]["control"]["digest"],
            )
            self.assertEqual(
                digest, yaml.safe_load(pinned_values.read_text())["images"]["control"]["digest"]
            )

    def test_standalone_scripts_are_syntax_checked_and_never_embed_secret_values(self) -> None:
        scripts = [
            ROOT / "scripts" / "bootstrap-standalone-secrets.sh",
            ROOT / "scripts" / "standalone.sh",
            ROOT / "scripts" / "cluster.sh",
        ]
        for script in scripts:
            subprocess.run(["bash", "-n", str(script)], check=True)
        bootstrap = scripts[0].read_text(encoding="utf-8")
        standalone = scripts[1].read_text(encoding="utf-8")
        self.assertIn("mktemp -d", bootstrap)
        self.assertIn("umask 077", bootstrap)
        self.assertIn("--from-file", bootstrap)
        self.assertNotIn("--from-literal", bootstrap)
        self.assertIn('get secret "$api_secret"', bootstrap)
        self.assertIn("existing values preserved", bootstrap)
        self.assertIn('namespaces.control=$namespace', standalone)
        self.assertIn('namespaces.gateway=$namespace', standalone)
        self.assertIn('--set-value)', standalone)
        self.assertIn('helm_set_value+=(--set', standalone)
        self.assertIn('"${helm_set_value[@]}"', standalone)
        self.assertNotIn("--take-ownership", standalone)
        self.assertNotIn("--force-conflicts", standalone)
        adapter = scripts[2].read_text(encoding="utf-8")
        self.assertIn("SITES_KUBE_CONTEXT", adapter)
        self.assertIn("SITES_CLUSTER_POD_CIDR", adapter)
        self.assertIn("SITES_LOCAL_PATH_PROVISIONER_ENABLED", adapter)
        self.assertIn("SITES_CONTROL_IMAGE_DIGEST", adapter)
        self.assertNotIn("INFRA_", adapter)
        self.assertNotIn("limactl", adapter)



if __name__ == "__main__":
    unittest.main()
