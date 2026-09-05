#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-up}
vm=${SITES_QUICKSTART_VM:-site-quickstart}
worker_count=${SITES_QUICKSTART_WORKERS:-2}
network=${SITES_QUICKSTART_NETWORK:-${vm}-net}
network_gateway=${SITES_QUICKSTART_NETWORK_GATEWAY:-192.168.107.1/24}
worker_cpus=${SITES_QUICKSTART_WORKER_CPUS:-2}
worker_memory=${SITES_QUICKSTART_WORKER_MEMORY_GIB:-3}
worker_disk=${SITES_QUICKSTART_WORKER_DISK_GIB:-20}
api_port=${SITES_QUICKSTART_KUBE_API_PORT:-18447}
console_port=${SITES_QUICKSTART_CONSOLE_PORT:-18091}
pod_cidr=${SITES_QUICKSTART_POD_CIDR:-10.201.0.0/16}
service_cidr=${SITES_QUICKSTART_SERVICE_CIDR:-10.202.0.0/16}
cilium_version=${SITES_QUICKSTART_CILIUM_VERSION:-1.19.6}
namespace=sites-local
state_dir="$root/.site-kubeadm"
kubeconfig="$state_dir/kubeconfig"
network_marker="$state_dir/network"
instances_dir="$state_dir/instances"
image=site-control:quickstart
context=site-quickstart
started_at=$SECONDS

[[ "$vm" =~ ^[a-z0-9][a-z0-9-]{0,58}$ ]] || {
  echo "SITES_QUICKSTART_VM must be a lowercase DNS label of at most 59 characters" >&2
  exit 2
}
[[ "$worker_count" =~ ^[1-4]$ ]] || {
  echo "SITES_QUICKSTART_WORKERS must be an integer from 1 through 4" >&2
  exit 2
}
[[ "$network" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "SITES_QUICKSTART_NETWORK must be a lowercase DNS label" >&2
  exit 2
}
[[ "$network_gateway" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.1/24$ ]] || {
  echo "SITES_QUICKSTART_NETWORK_GATEWAY must be an IPv4 /24 gateway ending in .1/24" >&2
  exit 2
}
for resource_value in "$worker_cpus" "$worker_memory" "$worker_disk"; do
  [[ "$resource_value" =~ ^[1-9][0-9]*$ ]] || {
    echo "worker CPU, memory, and disk settings must be positive integers" >&2
    exit 2
  }
done

worker_vms=()
for index in $(seq 1 "$worker_count"); do
  worker_vms+=("${vm}-w${index}")
done
all_worker_vms=()
for index in $(seq 1 4); do
  all_worker_vms+=("${vm}-w${index}")
done

usage() {
  cat <<'EOF'
Usage: scripts/quickstart-kubeadm.sh <doctor|up|scale|status|access|token|clean>

doctor  Check every host dependency and fixed local port before installation.
up      Create one control-plane and two worker Lima VMs, bootstrap Kubernetes with kubeadm,
        build Site from this checkout, deploy the example, and prove it.
scale   Reconcile the running cluster to SITES_QUICKSTART_WORKERS (1-4, default 2).
status  Show the node, Site workloads, example, verification, and monitoring.
access  Forward and open the console at http://127.0.0.1:18091/console/.
token   Print the disposable trial's local admin token for console login.
clean   Delete only the site-quickstart VMs, network, and this checkout's local state.

Prerequisites: Lima, a running Docker daemon, kubectl, Helm, curl, uv, lsof,
and Python 3.12+. The default three-node topology allocates 8 CPUs, 10 GiB RAM,
and 70 GiB of sparse disk across its VMs.
No other repository, pre-created Lima network, or published Site image is used.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 2
  }
}

check_prerequisites() {
  local command_name missing=()
  for command_name in limactl docker kubectl helm curl uv python3 lsof; do
    command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
  done
  if ((${#missing[@]})); then
    printf 'quickstart prerequisites are missing: %s\n' "${missing[*]}" >&2
    echo 'Install guide: https://github.com/hullwork/site#host-prerequisites' >&2
    exit 2
  fi

  python3 - <<'PY' || exit 2
import sys
if sys.version_info < (3, 12):
    raise SystemExit(
        f"Python 3.12+ is required; found {sys.version.split()[0]}. "
        "See https://www.python.org/downloads/"
    )
PY

  if ! docker info >/dev/null 2>&1; then
    echo 'Docker is installed, but its daemon is not reachable.' >&2
    echo 'Start Docker Desktop (macOS) or the Docker service (Linux), then rerun make quickstart.' >&2
    exit 2
  fi

  # These are the complete fixed-port contract of the reference VM. Checking
  # all of them now avoids a Lima forwarding failure several minutes later.
  if ! vm_running; then
    local port busy=()
    for port in "$api_port" "$console_port" 18090 18092 18093 18094 18095 18096 18097 18098; do
      if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        busy+=("$port")
      fi
    done
    if ((${#busy[@]})); then
      printf 'quickstart host ports are already in use: %s\n' "${busy[*]}" >&2
      echo 'Stop the process or older Site trial using those ports, then rerun make quickstart.' >&2
      exit 2
    fi
  fi

  cat <<EOF
Quickstart doctor passed
  commands: available
  Python: 3.12 or newer
  Docker daemon: reachable
  local ports: available or owned by this trial
  topology: 1 control-plane + $worker_count workers ($((worker_count + 1)) kubeadm nodes)
  control-plane allocation: 4 CPUs, 4 GiB RAM, 30 GiB sparse disk
  worker allocation: ${worker_cpus} CPUs, ${worker_memory} GiB RAM, ${worker_disk} GiB sparse disk each
  total allocation: $((4 + worker_count * worker_cpus)) CPUs, $((4 + worker_count * worker_memory)) GiB RAM, $((30 + worker_count * worker_disk)) GiB sparse disk
  network: outbound HTTPS is required while Lima, Kubernetes, Cilium, and workload images download
EOF
}

vm_exists() {
  limactl list --quiet | grep -Fxq "$vm"
}

vm_running() {
  vm_exists && limactl list "$vm" --format '{{.Status}}' 2>/dev/null | grep -Fxq Running
}

instance_exists() {
  limactl list --quiet | grep -Fxq "$1"
}

instance_owned() {
  [[ -f "$instances_dir/$1" ]]
}

mark_instance_owned() {
  mkdir -p "$instances_dir"
  : >"$instances_dir/$1"
}

network_exists() {
  limactl network list --json 2>/dev/null | NETWORK_NAME="$network" python3 -c '
import json, os, sys
name = os.environ["NETWORK_NAME"]
raise SystemExit(0 if any(json.loads(line).get("name") == name for line in sys.stdin if line.strip()) else 1)
'
}

guest_ip() {
  limactl shell "$1" -- sh -lc \
    "ip route get 1.1.1.1 | sed -n 's/.* src \\([^ ]*\\).*/\\1/p' | head -1"
}

ensure_guest_hostname() {
  local instance=$1
  if ! limactl shell "$instance" -- getent hosts "$instance" >/dev/null 2>&1; then
    printf '127.0.1.1 %s\n' "$instance" \
      | limactl shell "$instance" -- sudo tee -a /etc/hosts >/dev/null
  fi
}

create_worker_instance() {
  local worker=$1
  if ! instance_exists "$worker"; then
    echo "      Creating worker VM $worker"
    limactl create --name="$worker" --tty=false \
      --cpus="$worker_cpus" --memory="$worker_memory" --disk="$worker_disk" \
      --network="lima:$network" --set='.portForwards = []' \
      "$root/dev/kubeadm/lima.yaml"
    mark_instance_owned "$worker"
  else
    instance_owned "$worker" || {
      echo "Lima VM $worker exists but is not owned by this checkout; choose SITES_QUICKSTART_VM" >&2
      exit 2
    }
    echo "      Reusing worker VM $worker"
  fi
  limactl start "$worker" --timeout=15m
  ensure_guest_hostname "$worker"
}

join_worker_nodes() {
  local join_command worker
  local join_args=()
  join_command=$(limactl shell "$vm" -- sudo kubeadm token create --ttl=2h --print-join-command)
  read -r -a join_args <<<"$join_command"
  for worker in "$@"; do
    if limactl shell "$worker" -- test -f /etc/kubernetes/kubelet.conf \
      && kube get node "$worker" >/dev/null 2>&1; then
      echo "      $worker is already joined"
    else
      if limactl shell "$worker" -- test -f /etc/kubernetes/kubelet.conf; then
        limactl shell "$worker" -- sudo kubeadm reset --force >/dev/null
      fi
      limactl shell "$worker" -- sudo ${join_args[@]+"${join_args[@]}"} --node-name="$worker"
    fi
    for _ in $(seq 1 60); do
      kube get node "$worker" >/dev/null 2>&1 && break
      sleep 1
    done
    kube label node "$worker" node-role.kubernetes.io/worker=worker --overwrite >/dev/null
    kube uncordon "$worker" >/dev/null 2>&1 || true
  done
}

verify_node_topology() {
  kube get nodes -o json | CONTROL_NODE="$vm" EXPECTED_WORKERS="${worker_vms[*]}" python3 -c '
import json, os, sys
items = {item["metadata"]["name"]: item for item in json.load(sys.stdin)["items"]}
control = os.environ["CONTROL_NODE"]
workers = os.environ["EXPECTED_WORKERS"].split()
expected = {control, *workers}
if set(items) != expected:
    raise SystemExit(f"expected nodes {sorted(expected)}, found {sorted(items)}")
for name in expected:
    ready = next((c["status"] for c in items[name]["status"]["conditions"] if c["type"] == "Ready"), None)
    if ready != "True":
        raise SystemExit(f"node {name} is not Ready")
taints = items[control].get("spec", {}).get("taints", [])
if not any(t.get("key") == "node-role.kubernetes.io/control-plane" and t.get("effect") == "NoSchedule" for t in taints):
    raise SystemExit("control-plane node must retain its NoSchedule taint")
'
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
  local control_ip old_context worker
  mkdir -p "$state_dir"
  chmod 0700 "$state_dir"

  if vm_exists && ! instance_owned "$vm"; then
    echo "Lima VM $vm exists but is not owned by this checkout; choose SITES_QUICKSTART_VM" >&2
    exit 2
  fi
  for worker in ${worker_vms[@]+"${worker_vms[@]}"}; do
    if instance_exists "$worker" && ! instance_owned "$worker"; then
      echo "Lima VM $worker exists but is not owned by this checkout; choose SITES_QUICKSTART_VM" >&2
      exit 2
    fi
  done

  if network_exists; then
    if [[ ! -f "$network_marker" ]] || [[ "$(cat "$network_marker")" != "$network" ]]; then
      printf 'Lima network %s already exists but is not owned by this checkout; choose SITES_QUICKSTART_NETWORK\n' "$network" >&2
      exit 2
    fi
    echo "[1/8] Reusing repository-owned Lima network $network"
  else
    echo "[1/8] Creating repository-owned Lima user-v2 network $network"
    limactl network create "$network" --mode=user-v2 --gateway="$network_gateway"
    printf '%s\n' "$network" >"$network_marker"
  fi

  if ! vm_exists; then
    if lsof -nP -iTCP:"$api_port" -sTCP:LISTEN >/dev/null 2>&1; then
      printf 'host port %s is already in use; set SITES_QUICKSTART_KUBE_API_PORT and update the Lima template forward\n' "$api_port" >&2
      exit 2
    fi
    echo "      Creating control-plane VM $vm"
    limactl create --name="$vm" --tty=false \
      --cpus=4 --memory=4 --disk=30 \
      --network="lima:$network" \
      --set=".portForwards[0].hostPort = $api_port" \
      "$root/dev/kubeadm/lima.yaml"
    mark_instance_owned "$vm"
  else
    echo "      Reusing control-plane VM $vm"
  fi
  limactl start "$vm" --timeout=15m

  for worker in ${worker_vms[@]+"${worker_vms[@]}"}; do
    create_worker_instance "$worker"
  done

  control_ip=$(guest_ip "$vm")
  [[ "$control_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "could not discover the kubeadm advertise address" >&2
    exit 1
  }

  # Lima assigns its final instance hostname after system provisioning. Add it
  # immediately before kubeadm so the preflight check is clean and deterministic.
  ensure_guest_hostname "$vm"

  if ! limactl shell "$vm" -- test -f /etc/kubernetes/admin.conf; then
    echo "[2/8] Bootstrapping the Kubernetes control plane with kubeadm"
    limactl shell "$vm" -- sudo kubeadm init \
      --apiserver-advertise-address="$control_ip" \
      --pod-network-cidr="$pod_cidr" \
      --service-cidr="$service_cidr" \
      --node-name="$vm"
  else
    echo "[2/8] kubeadm control plane is already initialized"
  fi

  limactl shell "$vm" -- sudo cat /etc/kubernetes/admin.conf >"$kubeconfig"
  chmod 0600 "$kubeconfig"
  kubectl config --kubeconfig "$kubeconfig" set-cluster kubernetes \
    --server="https://127.0.0.1:$api_port" --tls-server-name="$control_ip" >/dev/null
  old_context=$(kubectl config --kubeconfig "$kubeconfig" current-context)
  if [[ "$old_context" != "$context" ]]; then
    kubectl config --kubeconfig "$kubeconfig" rename-context "$old_context" "$context" >/dev/null
  fi

  echo "[3/8] Joining $worker_count worker nodes"
  join_worker_nodes ${worker_vms[@]+"${worker_vms[@]}"}

  echo "[4/8] Installing the pinned Cilium pod network on every node"
  helm upgrade --install cilium oci://quay.io/cilium/charts/cilium \
    --version "$cilium_version" --namespace kube-system \
    --kubeconfig "$kubeconfig" --kube-context "$context" \
    --set routingMode=tunnel --set tunnelProtocol=vxlan \
    --set ipam.mode=kubernetes --set kubeProxyReplacement=false \
    --set operator.replicas=1 --wait --timeout=10m
  kube wait --for=condition=Ready nodes --all --timeout=10m

  verify_node_topology
}

scale_workers() {
  local desired worker index existing_node result phase verification public_url public_sha proof_sha app_node
  desired=$worker_count
  [[ -f "$kubeconfig" ]] || { echo "quickstart is not installed; run make quickstart first" >&2; exit 1; }
  vm_running || { echo "control-plane VM $vm is not running; run make quickstart first" >&2; exit 1; }
  [[ -f "$network_marker" ]] && [[ "$(cat "$network_marker")" == "$network" ]] && network_exists || {
    echo "repository-owned Lima network $network is unavailable; refusing to change nodes" >&2
    exit 1
  }

  echo "Scaling Site kubeadm workers to $desired"
  for worker in ${worker_vms[@]+"${worker_vms[@]}"}; do
    create_worker_instance "$worker"
  done
  join_worker_nodes ${worker_vms[@]+"${worker_vms[@]}"}

  # Every node may receive a control-plane or tenant Pod after a drain. Load the
  # checkout image before changing placement so a scale operation cannot strand it.
  docker build -t "$image" "$root"
  for worker in ${worker_vms[@]+"${worker_vms[@]}"}; do
    docker save "$image" | limactl shell "$worker" -- sudo ctr -n k8s.io images import - >/dev/null
  done

  for ((index=4; index>desired; index--)); do
    worker="${vm}-w${index}"
    if kube get node "$worker" >/dev/null 2>&1; then
      echo "      Draining $worker before removal"
      # Quickstart local volumes are constrained to w1. Refuse removal if that
      # invariant has been bypassed instead of orphaning a local persistent volume.
      kube get pv -o json | REMOVE_NODE="$worker" python3 -c '
import json, os, sys
node = os.environ["REMOVE_NODE"]
for pv in json.load(sys.stdin).get("items", []):
    terms = pv.get("spec", {}).get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
    for term in terms:
        for expr in term.get("matchExpressions", []):
            if expr.get("key") == "kubernetes.io/hostname" and node in expr.get("values", []):
                name = pv.get("metadata", {}).get("name", "<unknown>")
                raise SystemExit(f"refusing to remove {node}: persistent volume {name} is pinned there")
'
      kube drain "$worker" --ignore-daemonsets --delete-emptydir-data --timeout=10m
      limactl shell "$worker" -- sudo kubeadm reset --force >/dev/null
      kube delete node "$worker" --wait=true --timeout=2m
    fi
    if instance_exists "$worker"; then
      instance_owned "$worker" || {
        echo "refusing to delete unowned Lima VM $worker" >&2
        exit 1
      }
      limactl delete --force "$worker"
      rm -f "$instances_dir/$worker"
    fi
  done

  kube wait --for=condition=Ready nodes --all --timeout=10m
  verify_node_topology

  for deployment in sites-api sites-operator sites-activator sites-registry sites-prometheus; do
    kube -n "$namespace" rollout status "deployment/$deployment" --timeout=10m
  done
  kube -n "$namespace" rollout status statefulset/sites-postgres --timeout=10m
  for _ in $(seq 1 120); do
    result=$(kube get sitedeployments.sites.local -A -o json)
    phase=$(printf '%s' "$result" | python3 -c '
import json, sys
items = [item for item in json.load(sys.stdin)["items"] if item["spec"].get("serviceName") == "hello-site"]
print(items[0].get("status", {}).get("phase", "") if len(items) == 1 else "")
')
    verification=$(printf '%s' "$result" | python3 -c '
import json, sys
items = [item for item in json.load(sys.stdin)["items"] if item["spec"].get("serviceName") == "hello-site"]
print(str(items[0].get("status", {}).get("verification", {}).get("ok", False)).lower() if len(items) == 1 else "false")
')
    [[ "$phase" == Running && "$verification" == true ]] && break
    sleep 1
  done
  [[ "$phase" == Running && "$verification" == true ]] || {
    echo "hello-site did not recover after worker scaling" >&2
    exit 1
  }
  public_url=$(printf '%s' "$result" | python3 -c '
import json, sys
item = next(item for item in json.load(sys.stdin)["items"] if item["spec"].get("serviceName") == "hello-site")
print(item["status"]["url"])
')
  proof_sha=$(printf '%s' "$result" | python3 -c '
import json, sys
item = next(item for item in json.load(sys.stdin)["items"] if item["spec"].get("serviceName") == "hello-site")
print(item["status"]["verification"]["bodySha256"])
')
  public_sha=$(curl --fail --silent --show-error "$public_url" | python3 -c \
    'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
  [[ "$public_sha" == "$proof_sha" ]] || {
    echo "hello-site public URL digest changed after worker scaling" >&2
    exit 1
  }
  app_node=$(kube get pods -A -o json | python3 -c '
import json, sys
pods = [item for item in json.load(sys.stdin)["items"] if item["metadata"]["name"].startswith("hello-site-") and item.get("status", {}).get("phase") == "Running"]
if len(pods) != 1:
    raise SystemExit(f"expected one running hello-site pod, found {len(pods)}")
print(pods[0]["spec"]["nodeName"])
')
  case " ${worker_vms[*]} " in
    *" $app_node "*) ;;
    *) echo "hello-site recovered on unexpected node $app_node" >&2; exit 1 ;;
  esac
  existing_node=$(kube get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')
  cat <<EOF
Worker scaling passed
  topology: 1 control-plane + $desired workers ($existing_node Ready)
  retained storage worker: ${vm}-w1
  hello-site node: $app_node
  public URL: $public_url (HTTP body digest verified)
  control-plane scheduling: protected by NoSchedule taint
EOF
}

prove_site() {
  local node
  echo "[5/8] Building Site from this checkout and loading it on every node"
  docker build -t "$image" "$root"
  for node in "$vm" ${worker_vms[@]+"${worker_vms[@]}"}; do
    docker save "$image" | limactl shell "$node" -- sudo ctr -n k8s.io images import - >/dev/null
  done

  echo "[6/8] Installing Site and local observability"
  KUBECONFIG="$kubeconfig" \
  SITES_KUBE_CONTEXT="$context" \
  "$root/scripts/standalone.sh" install \
    --set images.control.repository=site-control \
    --set images.control.tag=quickstart \
    --set "localPathProvisioner.allowedNodeNames[0]=${vm}-w1" \
    --set-value monitoring.enabled=true
  # A truly new node may need several minutes to pull the pinned database,
  # registry, proxy, and Prometheus images. Do not spend the API rollout budget
  # while its dependencies are still downloading.
  for workload in \
    statefulset/sites-postgres \
    deployment/sites-registry \
    deployment/sites-prometheus; do
    kube -n "$namespace" rollout status "$workload" --timeout=15m
  done
  # The development tag intentionally stays stable. Helm therefore sees no Pod
  # template change on a warm rerun even though containerd now has a new image;
  # restart every control-image consumer so a successful rerun proves this checkout.
  kube -n "$namespace" rollout restart \
    deployment/sites-api deployment/sites-operator deployment/sites-activator
  for deployment in sites-api sites-operator sites-activator; do
    kube -n "$namespace" rollout status "deployment/$deployment" --timeout=10m
  done
  KUBECONFIG="$kubeconfig" SITES_KUBE_CONTEXT="$context" \
    "$root/scripts/standalone.sh" smoke

  local forward_log forward_pid forward_port api_url token result phase verification sha status_code source public_url public_sha app_node ready_nodes
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

  echo "[7/8] Deploying the included local/local application and waiting for measured HTTP proof"
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

  echo "[8/8] Verifying multi-node placement, persisted evidence, and Prometheus"
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

  app_node=$(kube get pods -A -o json | python3 -c '
import json, sys
pods = [
    item for item in json.load(sys.stdin)["items"]
    if item["metadata"]["name"].startswith("hello-site-")
    and item.get("status", {}).get("phase") == "Running"
]
if len(pods) != 1:
    raise SystemExit(f"expected one running hello-site pod, found {len(pods)}")
print(pods[0]["spec"]["nodeName"])
')
  case " ${worker_vms[*]} " in
    *" $app_node "*) ;;
    *) printf 'hello-site was scheduled on %s instead of a worker node\n' "$app_node" >&2; exit 1 ;;
  esac
  kube -n "$namespace" get pods -o json | CONTROL_NODE="$vm" python3 -c '
import json, os, sys
control = os.environ["CONTROL_NODE"]
pods = json.load(sys.stdin)["items"]
bad = [item["metadata"]["name"] for item in pods if item.get("spec", {}).get("nodeName") == control]
if bad:
    raise SystemExit(f"application control workloads escaped onto the control-plane node: {bad}")
'
  ready_nodes=$(kube get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')
  [[ "$ready_nodes" -eq $((worker_count + 1)) ]] || {
    printf 'expected %s Ready nodes, found %s\n' "$((worker_count + 1))" "$ready_nodes" >&2
    exit 1
  }

  cleanup_forward
  trap - EXIT
  cat <<EOF

Site kubeadm quickstart passed
  elapsed: $((SECONDS - started_at))s
  topology: 1 control-plane + $worker_count workers ($ready_nodes Ready)
  example node: $app_node
  control-plane scheduling: protected by NoSchedule taint
  phase: $phase
  HTTP verification: $status_code
  body SHA-256: $sha
  public URL: $public_url
  public URL body: matches verification digest
  admin deployment evidence: persisted
  observability: available

Next:
  terminal 1: make quickstart-access   # keep it running; opens http://127.0.0.1:$console_port/console/
  terminal 2: make quickstart-token    # paste into "管理员 token / Admin token"
  choose "进入控制台 / Enter the console"
  make quickstart-status
  SITES_QUICKSTART_WORKERS=3 make quickstart-scale  # resize workers in place (1-4)
  make quickstart-clean    # permanently deletes these disposable VMs and their applications
EOF
}

show_status() {
  [[ -f "$kubeconfig" ]] || { echo "quickstart is not installed; run make quickstart" >&2; exit 1; }
  kube get nodes -o wide
  kube -n "$namespace" get deployment,statefulset,service
  kube -n "$namespace" get pods -o wide
  kube get pods -A -o wide | awk 'NR == 1 || $2 ~ /^hello-site-/'
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
  2. Paste that value into "管理员 token / Admin token".
  3. Choose "进入控制台 / Enter the console".

The example application is under "应用 / Applications" as local/local/hello-site.
Its "打开 / Open" action reaches the public URL printed by make quickstart.
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
  local worker
  require_command limactl
  for worker in ${all_worker_vms[@]+"${all_worker_vms[@]}"}; do
    if instance_exists "$worker" && instance_owned "$worker"; then
      limactl delete --force "$worker"
    fi
  done
  if vm_exists && instance_owned "$vm"; then
    limactl delete --force "$vm"
  fi
  if [[ -f "$network_marker" ]] && [[ "$(cat "$network_marker")" == "$network" ]] && network_exists; then
    limactl network delete --force "$network"
  fi
  if [[ -d "$state_dir" ]]; then
    rm -rf -- "$state_dir"
  fi
  echo "removed only the $vm cluster VMs, repository-owned $network network, and $state_dir"
}

case "$action" in
  doctor) check_prerequisites ;;
  up)
    check_prerequisites
    create_cluster
    prove_site
    ;;
  scale)
    check_prerequisites
    scale_workers
    ;;
  status) require_command kubectl; show_status ;;
  access) require_command kubectl; access_console ;;
  token) require_command kubectl; show_token ;;
  clean) clean ;;
  --help|-h) usage ;;
  *) printf 'unknown command: %s\n' "$action" >&2; usage >&2; exit 2 ;;
esac
