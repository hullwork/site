# Standalone Helm lifecycle

This path installs site into the Kubernetes context selected by `kubectl`.
It does not require infra, agent, or their CI/CD pipelines. Review the
current context before continuing:

```bash
kubectl config current-context
helm lint charts/site -f charts/site/values-dev.yaml
```

For a disposable development cluster, generate local credentials and install:

```bash
make standalone-install
make standalone-smoke
```

The bootstrap helper generates high-entropy values in a mode-0700 temporary
directory only for missing Secrets and removes the temporary files on exit.
Existing credentials are preserved; an existing Secret missing a required key
fails closed instead of being silently replaced. The helper never writes
credentials into the repository or a Helm values file.
The smoke target waits for PostgreSQL, registry, API, and operator rollouts, then
port-forwards the API and requires `/readyz` to succeed.

For any existing cluster, the generic `scripts/cluster.sh up|status|verify`
surface accepts `KUBECONFIG`, optional `SITES_KUBE_CONTEXT`, a Helm values file,
and optional immutable control-image reference. Cluster-provider integration is
kept outside site.

For production, do not run the development bootstrap. Provision the Secrets and
keys documented in [Deployment](DEPLOYMENT.md#standalone-helm-installation) with
your secret manager, then install with an environment-specific values file:

```bash
helm upgrade --install site charts/site \
  --namespace sites --create-namespace --values values-production.yaml
```

Released charts include a companion `site-values-<version>.yaml`; use it to
pin the control image by digest when installing an OCI release.

To remove only the Helm-managed resources:

```bash
make standalone-uninstall
```

Uninstall intentionally preserves the namespace, externally managed Secrets, and
persistent volume claims. Delete retained state only after independently backing
it up and confirming that it is no longer needed.
