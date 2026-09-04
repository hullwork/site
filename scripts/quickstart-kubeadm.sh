#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-up}
vm=${SITES_QUICKSTART_VM:-site-quickstart}
api_port=${SITES_QUICKSTART_KUBE_API_PORT:-18447}
console_port=${SITES_QUICKSTART_CONSOLE_PORT:-18091}
pod_cidr=${SITES_QUICKSTART_POD_CIDR:-10.201.0.0/16}
service_cidr=${SITES_QUICKSTART_SERVICE_CIDR:-10.202.0.0/16}
cilium_version=${SITES_QUICKSTART_CILIUM_VERSION:-1.19.6}
namespace=sites-local
state_dir="$root/.site-kubeadm"
kubeconfig="$state_dir/kubeconfig"
image=site-control:quickstart
context=site-quickstart
started_at=$SECONDS

[[ "$vm" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "SITES_QUICKSTART_VM must be a lowercase DNS label" >&2
  exit 2
}

usage() {
  cat <<'EOF'
Usage: scripts/quickstart-kubeadm.sh <up|status|access|token|clean>

up      Create one disposable Lima VM, bootstrap Kubernetes with kubeadm,
        build Site from this checkout, deploy the example, and prove it.
status  Show the node, Site workloads, example, verification, and monitoring.
access  Forward and open the console at http://127.0.0.1:18091/console/.
token   Print the disposable trial's local admin token for console login.
clean   Delete only the site-quickstart VM and this checkout's .site-kubeadm state.

Prerequisites: Lima, Docker, kubectl, Helm, curl, uv, and Python 3.12+.
No other repository, pre-created Lima network, or published Site image is used.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 2
  }
}

vm_exists() {
  limactl list --quiet | grep -Fxq "$vm"
}

kube() {
  kubectl --kubeconfig "$kubeconfig" --context "$context" "$@"
}

admin_token() {
  kube -n "$namespace" get secret sites-api-token \
    -o jsonpath='{.data.token}' | base64 --decode
}

ensure_demo_tenant() {
  local token=$1 api_url=$2 tenants
  tenants=$(curl --fail --silent -H "X-Sites-Service-Token: $1" \
    "$api_url/v1/tenants?merchantId=local")
  if printf '%s' "$tenants" | python3 -c '
import json, sys
rows = json.load(sys.stdin).get("tenants", [])
raise SystemExit(0 if any(row.get("merchantId") == "local" and row.get("userId") == "local" for row in rows) else 1)
'; then
    return
  fi
  curl --fail --silent \
    -H "X-Sites-Service-Token: $token" \
    -H 'Content-Type: application/json' \
    -d '{"merchantId":"local","userId":"local"}' \
    "$api_url/v1/tenants" | python3 -c '
import json, sys
row = json.load(sys.stdin)
if row.get("merchantId") != "local" or row.get("userId") != "local" or not row.get("token"):
    raise SystemExit("quickstart tenant was not created")
'
}

wait_for_api_forward() {
  local log=$1 pid=$2 port=$3
  for _ in $(seq 1 100); do
    if curl --fail --silent "http://127.0.0.1:$port/readyz" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      cat "$log" >&2
      return 1
    fi
    sleep 0.2
  done
  cat "$log" >&2
  return 1
}

create_cluster() {
  mkdir -p "$state_dir"
  chmod 0700 "$state_dir"
  if ! vm_exists; then
    if lsof -nP -iTCP:"$api_port" -sTCP:LISTEN >/dev/null 2>&1; then
      printf 'host port %s is already in use; set SITES_QUICKSTART_KUBE_API_PORT and update the Lima template forward\n' "$api_port" >&2
      exit 2
    fi
    echo "[1/7] Creating the disposable Lima VM $vm"
    limactl create --name="$vm" --tty=false \
      --set=".portForwards[0].hostPort = $api_port" \
      "$root/dev/kubeadm/lima.yaml"
  else
    echo "[1/7] Reusing the repository-owned Lima VM $vm"
  fi
  limactl start "$vm" --timeout=15m

  local guest_ip
  guest_ip=$(limactl shell "$vm" -- sh -lc \
    "ip route get 1.1.1.1 | sed -n 's/.* src \\([^ ]*\\).*/\\1/p' | head -1")
  [[ "$guest_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "could not discover the kubeadm advertise address" >&2
    exit 1
  }

  # Lima assigns its final instance hostname after system provisioning. Add it
  # immediately before kubeadm so the preflight check is clean and deterministic.
  if ! limactl shell "$vm" -- getent hosts "$vm" >/dev/null 2>&1; then
    printf '127.0.1.1 %s\n' "$vm" \
      | limactl shell "$vm" -- sudo tee -a /etc/hosts >/dev/null
  fi

  if ! limactl shell "$vm" -- test -f /etc/kubernetes/admin.conf; then
    echo "[2/7] Bootstrapping Kubernetes with kubeadm"
    limactl shell "$vm" -- sudo kubeadm init \
      --apiserver-advertise-address="$guest_ip" \
      --pod-network-cidr="$pod_cidr" \
      --service-cidr="$service_cidr" \
      --node-name="$vm"
  else
    echo "[2/7] kubeadm is already initialized"
  fi

  limactl shell "$vm" -- sudo cat /etc/kubernetes/admin.conf >"$kubeconfig"
  chmod 0600 "$kubeconfig"
  kubectl config --kubeconfig "$kubeconfig" set-cluster kubernetes \
    --server="https://127.0.0.1:$api_port" --tls-server-name="$guest_ip" >/dev/null
  local old_context
  old_context=$(kubectl config --kubeconfig "$kubeconfig" current-context)
  if [[ "$old_context" != "$context" ]]; then
    kubectl config --kubeconfig "$kubeconfig" rename-context "$old_context" "$context" >/dev/null
  fi

  echo "[3/7] Installing the pinned Cilium pod network"
  helm upgrade --install cilium oci://quay.io/cilium/charts/cilium \
    --version "$cilium_version" --namespace kube-system \
    --kubeconfig "$kubeconfig" --kube-context "$context" \
    --set routingMode=tunnel --set tunnelProtocol=vxlan \
    --set ipam.mode=kubernetes --set kubeProxyReplacement=false \
    --set operator.replicas=1 --wait --timeout=10m
  kube taint nodes "$vm" node-role.kubernetes.io/control-plane- >/dev/null 2>&1 || true
  kube wait --for=condition=Ready "node/$vm" --timeout=5m
}

prove_site() {
  echo "[4/7] Building Site from this checkout"
  docker build -t "$image" "$root"
  docker save "$image" | limactl shell "$vm" -- sudo ctr -n k8s.io images import - >/dev/null

  echo "[5/7] Installing Site and local observability"
  KUBECONFIG="$kubeconfig" \
  SITES_KUBE_CONTEXT="$context" \
  "$root/scripts/standalone.sh" install \
    --set images.control.repository=site-control \
    --set images.control.tag=quickstart \
    --set-value monitoring.enabled=true
  # The development tag intentionally stays stable. Helm therefore sees no Pod
  # template change on a warm rerun even though containerd now has a new image;
  # restart every control-image consumer so a successful rerun proves this checkout.
  kube -n "$namespace" rollout restart \
    deployment/sites-api deployment/sites-operator deployment/sites-activator
  for deployment in sites-api sites-operator sites-activator; do
    kube -n "$namespace" rollout status "deployment/$deployment" --timeout=5m
  done
  KUBECONFIG="$kubeconfig" SITES_KUBE_CONTEXT="$context" \
    "$root/scripts/standalone.sh" smoke

  local forward_log forward_pid forward_port api_url token result phase verification sha status_code source public_url public_sha
  forward_log=$(mktemp "${TMPDIR:-/tmp}/site-api-forward.XXXXXX")
  kube -n "$namespace" port-forward service/sites-api :8080 >"$forward_log" 2>&1 &
  forward_pid=$!
  cleanup_forward() {
    if [[ -n "${forward_pid:-}" ]]; then
      kill "$forward_pid" 2>/dev/null || true
      wait "$forward_pid" 2>/dev/null || true
    fi
    [[ -z "${forward_log:-}" ]] || rm -f "$forward_log"
  }
  trap cleanup_forward EXIT
  for _ in $(seq 1 100); do
    forward_port=$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\) .*/\1/p' "$forward_log" | head -1)
    [[ -n "$forward_port" ]] && break
    if ! kill -0 "$forward_pid" 2>/dev/null; then
      cat "$forward_log" >&2
      exit 1
    fi
    sleep 0.1
  done
  [[ -n "$forward_port" ]] || { cat "$forward_log" >&2; echo "API port-forward did not report its local port" >&2; exit 1; }
  api_url="http://127.0.0.1:$forward_port"
  wait_for_api_forward "$forward_log" "$forward_pid" "$forward_port"
  token=$(admin_token)
  ensure_demo_tenant "$token" "$api_url"

  echo "[6/7] Deploying the included local/local application and waiting for measured HTTP proof"
  SITES_URL="$api_url" SITES_TOKEN="$token" \
    uv run --locked sites deploy-static --name hello-site \
      --directory "$root/examples/hello-site" --exposure public >/dev/null
  for _ in $(seq 1 120); do
    result=$(SITES_URL="$api_url" SITES_TOKEN="$token" \
      uv run --locked sites status hello-site 2>/dev/null || true)
    phase=$(printf '%s' "$result" | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("phase",""))' 2>/dev/null || true)
    verification=$(printf '%s' "$result" | python3 -c \
      'import json,sys; print(str(json.load(sys.stdin).get("verification",{}).get("ok",False)).lower())' 2>/dev/null || true)
    if [[ "$phase" == Running && "$verification" == true ]]; then break; fi
    sleep 1
  done
  [[ "$phase" == Running && "$verification" == true ]] || {
    printf 'example did not produce verification evidence:\n%s\n' "$result" >&2
    exit 1
  }
  sha=$(printf '%s' "$result" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["verification"]["bodySha256"])')
  status_code=$(printf '%s' "$result" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["verification"]["httpStatus"])')
  public_url=$(printf '%s' "$result" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("url", ""))')
  [[ "$public_url" == http://127.0.0.1:* ]] || {
    printf 'quickstart did not return a local public URL: %s\n' "$public_url" >&2
    exit 1
  }
  public_sha=$(curl --fail --silent --show-error "$public_url" | python3 -c \
    'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  [[ "$public_sha" == "$sha" ]] || {
    printf 'public URL body digest %s does not match control-plane proof %s\n' "$public_sha" "$sha" >&2
    exit 1
  }

  echo "[7/7] Verifying persisted admin evidence and Prometheus availability"
  for _ in $(seq 1 60); do
    source=$(curl --fail --silent -H "X-Sites-Service-Token: $token" \
      "$api_url/v1/admin/metrics/cluster" | python3 -c \
      'import json,sys; print(str(json.load(sys.stdin).get("source",{}).get("available",False)).lower())' \
      2>/dev/null || true)
    [[ "$source" == true ]] && break
    sleep 1
  done
  [[ "$source" == true ]] || { echo "Prometheus did not become available" >&2; exit 1; }
  curl --fail --silent -H "X-Sites-Service-Token: $token" \
    "$api_url/v1/admin/deployments" | python3 -c '
import json, sys
rows = json.load(sys.stdin).get("deployments", [])
row = next((item for item in rows if item.get("serviceName") == "hello-site"), None)
if not row or not row.get("verification", {}).get("ok") or not row.get("artifactSha256"):
    raise SystemExit("admin deployment snapshot is missing persisted proof")
'

  cleanup_forward
  trap - EXIT
  cat <<EOF

Site kubeadm quickstart passed
  elapsed: $((SECONDS - started_at))s
  phase: $phase
  HTTP verification: $status_code
  body SHA-256: $sha
  public URL: $public_url
  public URL body: matches verification digest
  admin deployment evidence: persisted
  observability: available

Next:
  make quickstart-access   # http://127.0.0.1:$console_port/console/
  make quickstart-token    # paste into the console login form
  make quickstart-status
  make quickstart-clean
EOF
}

show_status() {
  [[ -f "$kubeconfig" ]] || { echo "quickstart is not installed; run make quickstart" >&2; exit 1; }
  kube get nodes -o wide
  kube -n "$namespace" get deployment,statefulset,pod,service
  kube -n "$namespace" get sitedeployments.sites.local \
    -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,HTTP:.status.verification.httpStatus,SHA256:.status.verification.bodySha256'
}

access_console() {
  [[ -f "$kubeconfig" ]] || { echo "quickstart is not installed; run make quickstart" >&2; exit 1; }
  local url log pid opener
  url="http://127.0.0.1:$console_port/console/"
  log=$(mktemp "${TMPDIR:-/tmp}/site-console-forward.XXXXXX")
  kube -n "$namespace" port-forward service/sites-api "$console_port":8080 >"$log" 2>&1 &
  pid=$!
  cleanup_access() {
    if [[ -n "${pid:-}" ]]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    [[ -z "${log:-}" ]] || rm -f "$log"
  }
  trap cleanup_access EXIT INT TERM
  wait_for_api_forward "$log" "$pid" "$console_port"

  opener=""
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 && opener="Browser opened automatically." || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 && opener="Browser opened automatically." || true
  fi

  cat <<EOF
Console ready: $url
$opener

First login:
  1. In another terminal, run: make quickstart-token
  2. Paste that value into "Admin token", then choose "Enter the console".

The example application is under "Applications" as local/local/hello-site.
Press Ctrl-C here to stop console access without stopping the cluster.
EOF
  wait "$pid" || true
}

show_token() {
  [[ -f "$kubeconfig" ]] || { echo "quickstart is not installed; run make quickstart" >&2; exit 1; }
  echo "Disposable local admin token; treat it as a secret." >&2
  admin_token
  printf '\n'
}

clean() {
  require_command limactl
  if vm_exists; then
    limactl delete --force "$vm"
  fi
  if [[ -d "$state_dir" ]]; then
    rm -rf -- "$state_dir"
  fi
  echo "removed only the $vm Lima VM and $state_dir"
}

case "$action" in
  up)
    for command_name in limactl docker kubectl helm curl uv python3 lsof; do
      require_command "$command_name"
    done
    create_cluster
    prove_site
    ;;
  status) require_command kubectl; show_status ;;
  access) require_command kubectl; access_console ;;
  token) require_command kubectl; show_token ;;
  clean) clean ;;
  --help|-h) usage ;;
  *) printf 'unknown command: %s\n' "$action" >&2; usage >&2; exit 2 ;;
esac
