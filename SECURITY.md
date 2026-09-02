# Security policy

## Supported versions

Security fixes target the latest published release and current `main`. Old commits and pre-release experiments are not supported.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/convee/site/security/advisories/new). Do not open a public issue for a vulnerability.

Include the commit or release, affected endpoint or component, tenant/merchant impact, reproduction steps, logs with secrets removed, and any mitigation you identified. Maintainers aim to acknowledge reports within five business days.

Do not include real API keys, tenant tokens, merchant keys, kubeconfigs, private image names, production hostnames, or tenant data.

## Security boundary

site is an alpha deployment control plane, not a public hosting platform.

- Authentication is mandatory for deployment, administration, and MCP access.
- Identity is two-part `(merchant, tenant)`; merchant keys can only address their own tenants, and tenant tokens cannot select another user.
- Quotas apply at tenant, merchant, and Kubernetes namespace levels, but they are resource controls rather than a billing guarantee.
- The control plane accepts secret references, never secret values.
- Source builds are bounded to UTF-8 text contexts and run through the isolated build plane; binary build contexts are not accepted.
- S3-compatible source storage must use HTTPS; object-storage credentials are read from Secret-backed files.
- The scale-to-zero activator rejects request bodies above its configured bounded limit.
- `status.verification` proves the generated URL returned a non-redirect 2xx response and records the body digest. It does not prove business correctness or public internet reachability.
- The reference kubeadm deployment is for local or controlled environments. Custom domains, request-level secrets, and public hosting are outside the current contract.

## Hardening expectations for operators

- Store tokens outside the working directory with `SITES_TOKEN_FILE` or `SITES_MERCHANT_KEY_FILE`.
- Run the control plane on a private network and put an authenticated, TLS-terminating edge in front when needed.
- Rotate merchant and tenant credentials through the supported admin flows.
- Restrict Kubernetes API and registry access to the control-plane service account.
- Review tenant namespace NetworkPolicies and confirm that the cluster CNI actually enforces them.
