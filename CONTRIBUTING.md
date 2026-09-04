# Contributing to site

Thanks for improving site. Keep changes scoped, contract-first, and safe for a control plane that handles tenant identity, quotas, Kubernetes resources, and deployment evidence.

## Development setup

Requirements:

- Python 3.12+
- [uv](https://docs.astral-sh/uv/)
- Docker for PostgreSQL-backed tests
- Helm, kubectl, and Lima for the optional kubeadm trial
- Node.js 22 and npm for console changes

```bash
git clone https://github.com/hullwork/site.git
cd site
uv sync --locked --extra dev
make test-db
make test
make test-db-down
```

Console checks:

```bash
npm --prefix console ci --ignore-scripts
npm --prefix console run lint
npm --prefix console run typecheck
npm --prefix console run build
```

Container manifest syntax:

```bash
docker build --check .
```

For the disposable, self-contained kubeadm trial:

```bash
make quickstart
make quickstart-status
make quickstart-access
make quickstart-token
make quickstart-clean
```

The clean target deletes only the `site-quickstart` Lima VM and repository-local
kubeconfig. For an existing cluster, use the lower-level lifecycle documented in
[`docs/STANDALONE.md`](docs/STANDALONE.md).

## Change expectations

- Preserve tenant isolation, quota enforcement, fail-closed exposure policy, and the `status.verification` evidence contract.
- Expose capability and quota changes through `/v1/capabilities`; CLI and MCP must derive behavior from that response instead of maintaining a second list.
- Add regression coverage for API, admission, tenancy, reconciliation, exposure, or contract changes.
- Keep the source buildable, testable, releasable, and runnable without another repository.
- Do not commit `.env` files, tokens, kubeconfigs, private registry credentials, generated console artifacts, or local cluster state.
- User-visible behavior or deployment changes require README, agent-contract, and deployment-guide updates in the same pull request.

## Pull-request checklist

- [ ] Focused summary and motivation
- [ ] Relevant unit and contract checks pass
- [ ] Console lint, typecheck, and build pass when UI files changed
- [ ] Kubernetes manifests and container build checks pass when deployment files changed
- [ ] Tenancy, quota, security, and rollback impact is stated
- [ ] Documentation and examples contain no private data
