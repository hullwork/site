# Documentation index

site is an independently released deployment control plane for agent-facing workloads. Start with the [README](../README.md), then use this index to find the authoritative contract.

| Document | Audience | Use it for |
| --- | --- | --- |
| [README](../README.md) | All users | Product scope, capability boundary, architecture, local deployment, and tests |
| [Changelog](../CHANGELOG.md) | All users | Notable user-facing and operational changes |
| [Authentication contract](AUTH.md) | Authors of any client | Credentials, revocation, acting for a subject, how merchants and tenants come to exist, refusals, compatibility |
| [Agent contract](AGENT_CONTRACT.md) | Agent and MCP hosts | Capability discovery, deployment forms, success evidence, and MCP setup |
| [Deployment and integration](DEPLOYMENT.md) | Operators and integrators | Local prerequisites, lifecycle commands, and consumer boundaries |
| [Standalone Helm lifecycle](STANDALONE.md) | Operators | Bootstrapping Secrets, installing, smoke-testing, and uninstalling the Chart on its own |
| [Architecture guide](ARCHITECTURE.md) | Contributors and reviewers | Responsibility boundaries, control flow, and review rules |
| [Configuration ownership](CONFIGURATION.md) | Operators and administrators | Which settings belong in code, GitOps, the admin console, or tenant requests |
| [Cluster benchmark report](BENCHMARK_REPORT_2026-09-01.md) | Maintainers and adopters | Reproducible 60-trial lifecycle results and limitations |
| [Contributing](../CONTRIBUTING.md) | Contributors | Development setup, validation commands, review expectations, and PR checklist |
| [Security policy](../SECURITY.md) | All users | Supported versions, private vulnerability reporting, and trust boundaries |
| [Support](../SUPPORT.md) | All users | Issue, question, and security routing |
| [Code of conduct](../CODE_OF_CONDUCT.md) | Community | Behavior expectations and reporting path |

## Documentation rules

- `/v1/capabilities` is the source of truth for deployment capabilities and quota fields; do not hard-code its current values in prompts or documents.
- Configuration and deployment examples must not include real tokens, merchant keys, kubeconfigs, production hostnames, or private registry credentials.
- API or manifest changes require contract, README, and deployment updates together when they affect users or operators.
- New documents must state audience, prerequisites, supported topology, verification, rollback, and limitations.
