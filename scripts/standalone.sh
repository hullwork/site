#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-}
if [[ -n "$action" ]]; then shift; fi
namespace=sites-local
release=site
values_file="$root/charts/site/values-dev.yaml"
helm_set=()
helm_set_value=()
context=${SITES_KUBE_CONTEXT:-}
helm_context=()
if [[ -n "$context" ]]; then
  helm_context=(--kube-context "$context")
fi

kube() {
  if [[ -n "$context" ]]; then
    kubectl --context "$context" "$@"
  else
    kubectl "$@"
  fi
}

usage() {
  cat <<'EOF'
Usage: standalone.sh <install|smoke|uninstall> [options]

Options:
  --namespace NAME  Kubernetes namespace (default: sites-local)
  --release NAME    Helm release name (default: site)
  --values FILE     Helm values file (default: charts/site/values-dev.yaml)
  --set KEY=VALUE   Additional Helm string value (repeatable)
  --set-value KEY=VALUE
                    Additional typed Helm value (repeatable; booleans/numbers)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) namespace=${2:?missing namespace}; shift 2 ;;
    --release) release=${2:?missing release}; shift 2 ;;
    --values) values_file=${2:?missing values file}; shift 2 ;;
    --set) helm_set+=(--set-string "${2:?missing Helm value}"); shift 2 ;;
    --set-value) helm_set_value+=(--set "${2:?missing Helm value}"); shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$action" in
  install)
    command -v helm >/dev/null || { echo "helm is required" >&2; exit 2; }
    "$root/scripts/bootstrap-standalone-secrets.sh" --namespace "$namespace"
    helm ${helm_context[@]+"${helm_context[@]}"} upgrade --install "$release" "$root/charts/site" \
      --namespace "$namespace" --create-namespace --values "$values_file" \
      --set-string "namespaces.control=$namespace" \
      --set-string "namespaces.gateway=$namespace" \
      ${helm_set_value[@]+"${helm_set_value[@]}"} \
      ${helm_set[@]+"${helm_set[@]}"}
    ;;
  smoke)
    command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 2; }
    command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
    kube -n "$namespace" rollout status statefulset/sites-postgres --timeout=5m
    kube -n "$namespace" rollout status deployment/sites-registry --timeout=5m
    kube -n "$namespace" rollout status deployment/sites-api --timeout=5m
    kube -n "$namespace" rollout status deployment/sites-operator --timeout=5m
    local_port=${SITES_SMOKE_PORT:-}
    forward_log=$(mktemp)
    forward_spec=${local_port:+$local_port:}8080
    if [[ -z "$local_port" ]]; then forward_spec=:8080; fi
    kube -n "$namespace" port-forward service/sites-api "$forward_spec" >"$forward_log" 2>&1 &
    forward_pid=$!
    cleanup_forward() {
      kill "$forward_pid" 2>/dev/null || true
      wait "$forward_pid" 2>/dev/null || true
      rm -f "$forward_log"
    }
    trap cleanup_forward EXIT
    for _ in $(seq 1 50); do
      if [[ -z "$local_port" ]]; then
        local_port=$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\) .*/\1/p' "$forward_log" | head -1)
      fi
      if [[ -n "$local_port" ]] && kill -0 "$forward_pid" 2>/dev/null; then break; fi
      if ! kill -0 "$forward_pid" 2>/dev/null; then
        cat "$forward_log" >&2
        echo "sites-api port-forward exited before becoming ready" >&2
        exit 1
      fi
      sleep 0.1
    done
    if [[ -z "$local_port" ]]; then
      cat "$forward_log" >&2
      echo "sites-api port-forward did not report its local port" >&2
      exit 1
    fi
    for _ in $(seq 1 30); do
      if curl --fail --silent --show-error "http://127.0.0.1:$local_port/readyz" >/dev/null; then
        echo "site standalone smoke passed"
        exit 0
      fi
      sleep 1
    done
    echo "sites-api /readyz did not become reachable" >&2
    exit 1
    ;;
  uninstall)
    command -v helm >/dev/null || { echo "helm is required" >&2; exit 2; }
    command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 2; }
    # SiteDeployment and SiteBuild carry operator-owned cleanup finalizers.
    # Delete them while the operator is still alive; Helm otherwise removes the
    # operator first and can wait forever for finalizers nobody can complete.
    kube -n "$namespace" delete sitedeployments.sites.local --all \
      --ignore-not-found --wait=true --timeout=5m
    kube -n "$namespace" delete sitebuilds.sites.local --all \
      --ignore-not-found --wait=true --timeout=5m
    helm ${helm_context[@]+"${helm_context[@]}"} uninstall "$release" --namespace "$namespace" --wait
    echo "release removed; namespace, Secrets, and persistent data were preserved"
    ;;
  *) usage >&2; exit 2 ;;
esac
