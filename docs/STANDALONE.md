# Standalone Helm lifecycle

For a first source-checkout trial, use the disposable kubeadm workflow from the repository
root:

```bash
make quickstart-doctor  # checks commands, Docker, Python, and fixed host ports
make quickstart
SITES_QUICKSTART_WORKERS=3 make quickstart-scale  # optional: resize to 1-4 workers
make quickstart-access   # terminal 1: opens and serves the console
make quickstart-token    # terminal 2: paste into 管理员 token / Admin token
make quickstart-clean    # deletes only the Site trial VMs, network, and local state
```

The default three VMs allocate 8 CPUs, 10 GiB RAM, and 70 GiB of sparse disk in total. They need
outbound HTTPS while the Ubuntu, Kubernetes, Cilium, and workload images download, plus
host ports 18090–18098 and 18447. A first uncached install may take 8–30 minutes.
`make quickstart` exits after printing the proof. Keep `make quickstart-access` running;
pressing Ctrl-C there closes only the console tunnel, not the cluster. In the default
Chinese UI choose **进入控制台**; in English choose **Enter the console**.

That path creates one control-plane and two worker Lima VMs, installs Kubernetes 1.36 with
kubeadm, joins the workers, installs a pinned Cilium release, and builds the control image
locally. The Helm release supplies
its own local storage provisioner, enables Prometheus, deploys `examples/hello-site`, and
gates success on the deployment's HTTP status/body digest, a host-openable public URL with
the same body digest, plus the admin metrics and deployment-snapshot APIs. It needs no
pre-created Lima network, external image, or other repository. After login, the included
application appears under **应用 / Applications** as
`local/local/hello-site`, including an **Open** action; use **Monitor → Single application**
for its resource samples. **Deploy application** creates or updates from an existing
container image: public applications receive a browser URL, while internal applications
are explicitly labeled as having no public URL.

`SITES_QUICKSTART_WORKERS=N make quickstart-scale` reconciles the live trial to one through
four workers. Scale-down drains the highest-numbered workers first. All local persistent
volumes are constrained to worker `w1`, which is retained; the command refuses removal if
it detects a local volume on a candidate node. This only scales workers: one control-plane
is not an HA topology.

The control-plane keeps its kubeadm `NoSchedule` taint. Site, Prometheus, and tenant Pods
run on workers; the quickstart fails if a Site workload crosses that boundary.

`make quickstart-clean` is intentionally destructive only to the disposable trial: it
deletes its VMs, repository-owned Lima network, every application inside it, and this
checkout's local kubeconfig.

The lower-level path installs Site into the Kubernetes context selected by `kubectl`.
Review the current context before continuing:

```bash
kubectl config current-context
helm lint charts/site -f charts/site/values-dev.yaml
```

For a cluster whose Pod CIDR and control image have already been configured in the
selected values, generate local credentials and install:

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
