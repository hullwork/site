#!/usr/bin/env bash
set -euo pipefail

# Generic Kubernetes/Helm lifecycle owned by site. Cluster creation,
# image transport and infrastructure-provider adapters intentionally live
# outside this repository.
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-}
namespace=${SITES_NAMESPACE:-sites-local}
release=${SITES_HELM_RELEASE:-site}
values_file=${SITES_HELM_VALUES:-$root/charts/site/values-dev.yaml}
context=${SITES_KUBE_CONTEXT:-}

usage() {
  cat <<'EOF'
Usage: scripts/cluster.sh <up|status|verify>

Standard inputs:
  KUBECONFIG                     Kubernetes client configuration.
  SITES_KUBE_CONTEXT             Optional context; defaults to current context.
  SITES_NAMESPACE                Control namespace (default: sites-local).
  SITES_HELM_RELEASE             Helm release (default: site).
  SITES_HELM_VALUES              Values file (default: values-dev.yaml).
  SITES_CLUSTER_POD_CIDR         Actual cluster Pod CIDR; overrides the values file.
  SITES_LOCAL_PATH_PROVISIONER_ENABLED
                                 true/false; set false when the cluster already has one.
  SITES_CONTROL_IMAGE_REPOSITORY Optional pre-published control image repository.
  SITES_CONTROL_IMAGE_TAG        Optional control image tag.
  SITES_CONTROL_IMAGE_DIGEST     Optional immutable sha256 digest.

The target cluster and referenced images must already exist. This command does
not create VMs, install a Kubernetes distribution, build images, or depend on an
infrastructure repository.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    return 2
  }
}

kube() {
  if [[ -n "$context" ]]; then
    kubectl --context "$context" "$@"
  else
    kubectl "$@"
  fi
}

helm_context_args=()
if [[ -n "$context" ]]; then
  helm_context_args=(--kube-context "$context")
fi

image_args=()
if [[ -n "${SITES_CONTROL_IMAGE_REPOSITORY:-}" ]]; then
  image_args+=(--set "images.control.repository=$SITES_CONTROL_IMAGE_REPOSITORY")
fi

cluster_args=()
if [[ -n "${SITES_CLUSTER_POD_CIDR:-}" ]]; then
  cluster_args+=(--set-string "clusterNetwork.podCIDR=$SITES_CLUSTER_POD_CIDR")
fi
if [[ -n "${SITES_LOCAL_PATH_PROVISIONER_ENABLED:-}" ]]; then
  case "$SITES_LOCAL_PATH_PROVISIONER_ENABLED" in
    true|false) ;;
    *)
      printf 'SITES_LOCAL_PATH_PROVISIONER_ENABLED must be true or false\n' >&2
      exit 2
      ;;
  esac
  cluster_args+=(--set "localPathProvisioner.enabled=$SITES_LOCAL_PATH_PROVISIONER_ENABLED")
fi
if [[ -n "${SITES_CONTROL_IMAGE_TAG:-}" ]]; then
  image_args+=(--set "images.control.tag=$SITES_CONTROL_IMAGE_TAG")
fi
if [[ -n "${SITES_CONTROL_IMAGE_DIGEST:-}" ]]; then
  [[ "$SITES_CONTROL_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    printf 'SITES_CONTROL_IMAGE_DIGEST must be sha256:<64 lowercase hex>\n' >&2
    exit 2
  }
  image_args+=(--set "images.control.digest=$SITES_CONTROL_IMAGE_DIGEST")
fi

up() {
  require_command kubectl
  require_command helm
  "$root/scripts/bootstrap-standalone-secrets.sh" --namespace "$namespace"
  helm ${helm_context_args[@]+"${helm_context_args[@]}"} upgrade --install "$release" \
    "$root/charts/site" --namespace "$namespace" --create-namespace \
    --values "$values_file" \
    --set "namespaces.control=$namespace" \
    --set "namespaces.gateway=$namespace" \
    ${cluster_args[@]+"${cluster_args[@]}"} \
    ${image_args[@]+"${image_args[@]}"}
  verify
}

status() {
  require_command kubectl
  kube get nodes
  kube -n "$namespace" get deployment,statefulset,pod,service
  kube -n "$namespace" get sitedeployments.sites.local \
    -o custom-columns='NAME:.metadata.name,MERCHANT:.spec.merchantID,TENANT:.spec.userID,PHASE:.status.phase,URL:.status.url' \
    2>/dev/null || true
}

verify() {
  require_command kubectl
  require_command curl
  "$root/scripts/standalone.sh" smoke --namespace "$namespace" \
    --release "$release" --values "$values_file"
}

case "$action" in
  up) up ;;
  status) status ;;
  verify) verify ;;
  --help|-h|'') usage ;;
  *) printf 'unknown command: %s\n' "$action" >&2; usage >&2; exit 2 ;;
esac
