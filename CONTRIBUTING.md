# Contributing to site

Thanks for improving site. Keep changes scoped, contract-first, and safe for a control plane that handles tenant identity, quotas, Kubernetes resources, and deployment evidence.

## Development setup

Requirements:

- Python 3.12+
- [uv](https://docs.astral-sh/uv/)
- Docker for PostgreSQL-backed tests
- Helm and kubectl for optional standalone cluster checks
- Node.js 22 and npm for console changes

```bash
git clone https://github.com/convee/site.git
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

For a self-contained cluster with a current kubectl context:

```bash
make standalone-install
make standalone-smoke
make standalone-uninstall
```

The uninstall target preserves the namespace, generated development Secrets, and PVCs.
Delete them separately only when you intend to destroy that local environment.

## Change expectations

- Preserve tenant isolation, quota enforcement, fail-closed exposure policy, and the `status.verification` evidence contract.
- Expose capability and quota changes through `/v1/capabilities`; CLI and MCP must derive behavior from that response instead of maintaining a second list.
- Add regression coverage for API, admission, tenancy, reconciliation, exposure, or contract changes.
- Keep source independent of the agent checkout. agent owns only its consumer wiring, not this repository's package, image, or manifests.
- Do not commit `.env` files, tokens, kubeconfigs, private registry credentials, generated console artifacts, or local cluster state.
- User-visible behavior or deployment changes require README, agent-contract, and deployment-guide updates in the same pull request.

## Pull-request checklist

- [ ] Focused summary and motivation
- [ ] Relevant unit and contract checks pass
- [ ] Console lint, typecheck, and build pass when UI files changed
- [ ] Kubernetes manifests and container build checks pass when deployment files changed
- [ ] Tenancy, quota, security, and rollback impact is stated
- [ ] Documentation and examples contain no private data
