#!/usr/bin/env bash
set -euo pipefail

namespace=sites-local
api_secret=sites-api-token
database_secret=sites-postgres-auth
registry_secret=sites-registry-auth
# In-cluster address the BuildKit Job pushes to, and therefore the key the
# generated docker config authenticates. Must equal SITES_REGISTRY_PUSH_HOST as
# the Chart renders it, or the push is a 401 nothing else explains. Resolved
# after argument parsing so --namespace decides it.
registry_host=${SITES_REGISTRY_PUSH_HOST:-}
context=${SITES_KUBE_CONTEXT:-}

kube() {
  if [[ -n "$context" ]]; then
    kubectl --context "$context" "$@"
  else
    kubectl "$@"
  fi
}

usage() {
  cat <<'EOF'
Usage: bootstrap-standalone-secrets.sh [options]

Generate development-only credentials and apply existing Secrets to the current
kubectl context. Plaintext values exist only in a mode-0700 temporary directory.

Options:
  --namespace NAME         Target namespace (default: sites-local)
  --api-secret NAME        API Secret name (default: sites-api-token)
  --database-secret NAME   PostgreSQL Secret name (default: sites-postgres-auth)
  --registry-secret NAME   Registry Secret name (default: sites-registry-auth)
  --registry-host HOST     Registry address the build plane pushes to
                           (default: sites-registry.<namespace>.svc:5000;
                           also read from SITES_REGISTRY_PUSH_HOST)
  --help                   Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) namespace=${2:?missing namespace}; shift 2 ;;
    --api-secret) api_secret=${2:?missing API Secret name}; shift 2 ;;
    --database-secret) database_secret=${2:?missing database Secret name}; shift 2 ;;
    --registry-secret) registry_secret=${2:?missing registry Secret name}; shift 2 ;;
    --registry-host) registry_host=${2:?missing registry host}; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in kubectl openssl; do
  command -v "$command_name" >/dev/null || {
    echo "$command_name is required" >&2
    exit 2
  }
done

case "$namespace" in
  ''|*[!a-z0-9.-]*|.*|*.) echo "invalid Kubernetes namespace: $namespace" >&2; exit 2 ;;
esac
if [[ ${#namespace} -gt 63 ]]; then
  echo "Kubernetes namespace must be at most 63 characters" >&2
  exit 2
fi

registry_host=${registry_host:-sites-registry.$namespace.svc:5000}

secret_dir=$(mktemp -d "${TMPDIR:-/tmp}/site-secrets.XXXXXX")
chmod 0700 "$secret_dir"
trap 'find "$secret_dir" -type f -exec chmod 0600 {} + 2>/dev/null || true; rm -rf -- "$secret_dir"' EXIT
umask 077

openssl rand -hex 32 >"$secret_dir/token"
openssl rand -hex 48 >"$secret_dir/console-session-key"
printf '%s' sites >"$secret_dir/username"
printf '%s' sites >"$secret_dir/database"
openssl rand -base64 36 | tr -d '\n' >"$secret_dir/database-password"
openssl rand -base64 36 | tr -d '\n' >"$secret_dir/registry-password"
registry_sha=$(openssl dgst -binary -sha1 <"$secret_dir/registry-password" | openssl base64 -A)
printf 'sites:{SHA}%s\n' "$registry_sha" >"$secret_dir/htpasswd"
# The third form of the same credential, for the BuildKit Job. It sets
# DOCKER_CONFIG to the directory this Secret is mounted at, and Docker looks for
# `config.json` there -- the name is Docker's, not a preference. Without this key
# the Job's volume cannot be set up at all, so the Pod never starts and the only
# thing the caller ever sees is the build deadline expiring several minutes
# later, naming nothing.
registry_auth=$(printf 'sites:%s' "$(cat "$secret_dir/registry-password")" | openssl base64 -A)
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$registry_host" "$registry_auth" \
  >"$secret_dir/registry-config.json"

kube create namespace "$namespace" --dry-run=client -o yaml | kube apply -f - >/dev/null

require_existing_keys() {
  local secret_name=$1 key value
  shift
  for key in "$@"; do
    value=$(kube -n "$namespace" get secret "$secret_name" \
      -o "go-template={{ index .data \"$key\" }}")
    if [[ -z "$value" ]]; then
      echo "existing Secret $secret_name is missing required key $key; refusing to rotate it implicitly" >&2
      return 1
    fi
  done
}

if kube -n "$namespace" get secret "$api_secret" >/dev/null 2>&1; then
  require_existing_keys "$api_secret" token console-session-key
else
  kube -n "$namespace" create secret generic "$api_secret" \
    --from-file=token="$secret_dir/token" \
    --from-file=console-session-key="$secret_dir/console-session-key" >/dev/null
fi
if kube -n "$namespace" get secret "$database_secret" >/dev/null 2>&1; then
  require_existing_keys "$database_secret" username database password
else
  kube -n "$namespace" create secret generic "$database_secret" \
    --from-file=username="$secret_dir/username" \
    --from-file=database="$secret_dir/database" \
    --from-file=password="$secret_dir/database-password" >/dev/null
fi
if kube -n "$namespace" get secret "$registry_secret" >/dev/null 2>&1; then
  require_existing_keys "$registry_secret" password htpasswd config.json
else
  kube -n "$namespace" create secret generic "$registry_secret" \
    --from-file=password="$secret_dir/registry-password" \
    --from-file=htpasswd="$secret_dir/htpasswd" \
    --from-file=config.json="$secret_dir/registry-config.json" >/dev/null
fi

echo "site development Secrets are present in namespace $namespace (existing values preserved)"
echo "retrieve the one-time local admin token with:"
echo "  kubectl -n $namespace get secret $api_secret -o jsonpath='{.data.token}' | base64 --decode"
