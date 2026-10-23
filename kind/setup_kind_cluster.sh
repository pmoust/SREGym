#!/usr/bin/env bash
# setup_kind_cluster.sh
# Creates a Kind cluster with Calico CNI for SREGym.
#
# Usage (from repo root):
#   bash kind/setup_kind_cluster.sh [arm|x86]
#
# Environment:
#   KIND_CLUSTER_NAME  cluster to create (default "kind"). Two clusters on one
#                      host need distinct names, or the second create fails.
#   KUBECONFIG         where kind writes the cluster's context. Point each
#                      concurrent run at its own file so the current-context
#                      every later kubectl resolves is unambiguous.
#   KIND_CONFIG        cluster config to create from, instead of the one this
#                      script picks by arch. The native benchmark arm passes a
#                      copy that binds the API server to the host's private
#                      address, which brain-go's Kubernetes connector needs and
#                      which nothing else does; unset, every caller gets the
#                      committed config byte for byte.
#   SREGYM_REGISTRY_MIRROR_ADDR
#                      Host-reachable address of the box's registry mirror
#                      containers (run_shard.sh exports it as the box's own
#                      RFC1918 address; see registry_mirror.sh). When set,
#                      every cluster this script creates (all three arms
#                      alike, since all three call this script) gets:
#                        1. A containerd config_path patch pointing at
#                           /etc/containerd/certs.d, with no "registry.mirrors"
#                           entry at all. containerd v2's CRI plugin does not
#                           accept both config_path and registry.mirrors in
#                           the same config, and registry.mirrors on this
#                           containerd version (v2.0.2, the kindest/node:
#                           v1.32.1 base image) appends the real upstream as a
#                           last fallback endpoint and silently falls through
#                           to it on a 404 instead of failing the pull, which
#                           defeats the whole point of a benchmark run never
#                           touching the public internet. Confirmed locally
#                           with `crictl pull` (the
#                           path kubelet actually uses; `ctr images pull`
#                           does NOT consult this config at all and is not a
#                           valid way to test it): a kind cluster patched
#                           with registry.mirrors pulled a real image that was
#                           absent from the mirror by falling through to real
#                           Docker Hub; the same pull against a config_path
#                           cluster failed outright ("not found"), with no
#                           internet access attempted.
#                        2. /etc/containerd/certs.d/<host>/hosts.toml for each
#                           of the four upstream hosts this benchmark mirrors
#                           (docker.io, ghcr.io, quay.io, registry.k8s.io),
#                           each containing only
#                           "server = \"http://<addr>:<port>\"" and no
#                           upstream [host] entry (confirmed locally; a bare
#                           "server" line is enough, and containerd then
#                           treats it as the sole endpoint for that host, no
#                           fallback), plus /etc/containerd/certs.d/_default/
#                           hosts.toml pointing at "http://127.0.0.1:1" (a
#                           loopback port nothing listens on), so any host
#                           NOT in that list of four fails the pull instead of
#                           falling through to config_path's own default,
#                           which is containerd's ordinary, unmediated
#                           upstream resolution, a real pull to the real
#                           internet. Confirmed locally: removing just the "_default"
#                           file let a pull from an unlisted host
#                           (public.ecr.aws) succeed for real; restoring it
#                           made that same pull fail closed with "connection
#                           refused".
#
#                           All five are generated and written before "kind
#                           create cluster" runs, then mounted onto every
#                           node's existing extraMounts list (the same
#                           mechanism kind-config-*.yaml already uses for
#                           /run/udev), not copied in with "docker cp"
#                           afterward: a node starts serving pulls the
#                           instant its container boots, before this script
#                           would otherwise get a chance to copy anything in,
#                           and a pod the API server schedules in that window
#                           (kube-proxy, a CNI daemon) would have pulled
#                           through whatever config_path resolved to at that
#                           moment (an empty or absent certs.d), i.e. the
#                           real internet. Mounting removes that window
#                           instead of narrowing it. If a future KIND_CONFIG
#                           ever has a node with no "extraMounts:" key to
#                           extend, this script fails loudly rather than
#                           falling back to copying in after boot and
#                           reopening that window.
#                      Unset (a developer's own machine, with no mirror
#                      running), every cluster is created byte-for-byte as
#                      before: no patch is added, and every pull goes
#                      straight to its real upstream, same as always.
#
# Requirements:
#   - kind
#   - kubectl

set -euo pipefail

CALICO_VERSION="v3.27.0"
ARCH="${1:-x86}"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-kind}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIND_CONFIG="${KIND_CONFIG:-${SCRIPT_DIR}/kind-config-${ARCH}.yaml}"

if [[ ! -f "${KIND_CONFIG}" ]]; then
    echo "❌ Config file not found: ${KIND_CONFIG}"
    echo "Usage: bash kind/setup_kind_cluster.sh [arm|x86]"
    exit 1
fi

# Add the registry-mirror patch on top of whatever KIND_CONFIG already is
# (the committed file, or native's own apiServerAddress-patched copy), rather
# than editing either in place: the committed file has to stay usable
# byte-for-byte with no mirror running, and native's copy is already a
# generated temp file nothing else reads back.
if [ -n "${SREGYM_REGISTRY_MIRROR_ADDR:-}" ]; then
    REGISTRY_MIRROR_SCRIPT="${SCRIPT_DIR}/../scripts/benchmark/registry_mirror.sh"
    if [ ! -f "${REGISTRY_MIRROR_SCRIPT}" ]; then
        echo "❌ SREGYM_REGISTRY_MIRROR_ADDR is set but ${REGISTRY_MIRROR_SCRIPT} is missing"
        exit 1
    fi

    # Generated and populated before "kind create cluster" runs (see the
    # env-var comment above for why). The EXIT trap here only covers the
    # failure path (a `set -e` death anywhere below, or "kind create
    # cluster" itself failing) and is deliberately disarmed right after
    # that call succeeds (see the comment there): once a node has this
    # directory bind-mounted, deleting it is not a no-op cleanup, it empties
    # the live mount every later pull on that node depends on, for as long
    # as the cluster runs.
    REGISTRY_CERTS_DIR="$(mktemp -d -t "sregym-kind-certs-${KIND_CLUSTER_NAME}.XXXXXX")"
    trap 'rm -rf "${REGISTRY_CERTS_DIR}"' EXIT

    # One hosts.toml per upstream host this benchmark actually mirrors.
    # "server" only, no "[host...]" table: confirmed locally that a bare
    # "server" line is enough and that containerd then treats it as the only
    # endpoint for that host, with no fallback.
    for host in $(bash "${REGISTRY_MIRROR_SCRIPT}" hosts); do
        port=$(bash "${REGISTRY_MIRROR_SCRIPT}" port "${host}")
        mkdir -p "${REGISTRY_CERTS_DIR}/${host}"
        printf 'server = "http://%s:%s"\n' "${SREGYM_REGISTRY_MIRROR_ADDR}" "${port}" \
            >"${REGISTRY_CERTS_DIR}/${host}/hosts.toml"
    done

    # "_default": config_path's own fallback for any host with no per-host
    # directory under certs.d is containerd's ordinary, unmediated upstream
    # resolution, a real pull to the real internet. The four hosts.toml
    # files above only cover the four upstreams this benchmark mirrors;
    # anything else (a fault that swaps in an image from a fifth host, a
    # typo, an image_cache.list addition that lands before its host is wired
    # up here) would otherwise pull from the internet with no failure at
    # all. A loopback port nothing listens on makes that pull fail instead:
    # confirmed locally that
    # removing this one file lets a pull from an unlisted host
    # (public.ecr.aws) succeed for real, and restoring it makes that same
    # pull fail closed with "connection refused".
    mkdir -p "${REGISTRY_CERTS_DIR}/_default"
    printf 'server = "http://127.0.0.1:1"\n' >"${REGISTRY_CERTS_DIR}/_default/hosts.toml"

    # A predictable /tmp path let a second concurrent call for the same
    # cluster name race on the same file; mktemp gives each call its own, so
    # two slots creating a cluster with the same name never clobber each
    # other's mid-write file. SREGYM_MIRRORED_KIND_
    # CONFIG_OUT still overrides this for a caller that wants a fixed, known
    # path (a test, a one-off debug run).
    MIRRORED_CONFIG="${SREGYM_MIRRORED_KIND_CONFIG_OUT:-$(mktemp -t "sregym-kind-mirrored-${KIND_CLUSTER_NAME}.XXXXXX")}"

    # Mount REGISTRY_CERTS_DIR onto every node's existing "extraMounts:" list
    # (the same list kind-config-*.yaml already uses for /run/udev) rather
    # than copying files in with "docker cp" after the cluster exists: a
    # node starts serving pulls the instant its container boots, so a copy
    # step run afterward leaves a window where an early pod (kube-proxy, a
    # CNI daemon) pulls with config_path set but no certs.d content behind
    # it yet, which is the same real-internet gap all over again. Matches on
    # the exact 4-space indent kind-config-*.yaml and native_arm.sh's
    # apiServerAddress-patched copy both use for a node's "extraMounts:" key
    # today.
    awk -v dir="${REGISTRY_CERTS_DIR}" '
        { print }
        /^    extraMounts:$/ {
            print "      - hostPath: " dir
            print "        containerPath: /etc/containerd/certs.d"
        }
    ' "${KIND_CONFIG}" >"${MIRRORED_CONFIG}"

    # config_path only, no "registry.mirrors": the two are mutually exclusive
    # in containerd's CRI registry config, and registry.mirrors is the form
    # that silently falls back to the real upstream on a miss (see the
    # env-var comment above).
    {
        echo "containerdConfigPatches:"
        echo "  - |"
        echo "    [plugins.\"io.containerd.grpc.v1.cri\".registry]"
        echo "      config_path = \"/etc/containerd/certs.d\""
    } >>"${MIRRORED_CONFIG}"

    echo "==> Registry mirror patch added (SREGYM_REGISTRY_MIRROR_ADDR=${SREGYM_REGISTRY_MIRROR_ADDR}): ${MIRRORED_CONFIG}"
    KIND_CONFIG="${MIRRORED_CONFIG}"

    # Fail loudly, not a silent fallback: every node in both committed
    # configs (kind-config-x86.yaml, kind-config-arm.yaml) already declares
    # its own "extraMounts:" list, so the awk pass above mounts the certs
    # directory into every one of them before node boot. If some future
    # KIND_CONFIG has a node with no "extraMounts:" key to extend, that node
    # would silently get no mount and no fail-closed default at all, a
    # worse outcome than stopping here and asking for an "extraMounts:" key
    # to be added to that node.
    # || true on both counts: grep -c exits 1 when the count is zero, which
    # under set -euo pipefail would kill the script at the assignment before
    # the diagnostics below can report the exact failure they exist for.
    nodes_total=$(grep -c '^  - role:' "${MIRRORED_CONFIG}" || true)
    nodes_mounted=$(grep -c '^        containerPath: /etc/containerd/certs.d$' "${MIRRORED_CONFIG}" || true)
    if [ "${nodes_total}" -eq 0 ]; then
        echo "❌ no '  - role:' node found in ${KIND_CONFIG}; cannot check the registry mirror mount before using SREGYM_REGISTRY_MIRROR_ADDR"
        exit 1
    fi
    if [ "${nodes_mounted}" -lt "${nodes_total}" ]; then
        echo "❌ ${nodes_mounted}/${nodes_total} nodes in ${KIND_CONFIG} have an 'extraMounts:' key to extend; add one to every node (see kind-config-x86.yaml) before using SREGYM_REGISTRY_MIRROR_ADDR"
        exit 1
    fi
fi

echo "==> Step 1: Create Kind cluster (arch: ${ARCH}, name: ${KIND_CLUSTER_NAME})"
kind create cluster --name "${KIND_CLUSTER_NAME}" --config "${KIND_CONFIG}"
# No post-create copy step: REGISTRY_CERTS_DIR, when set, is already mounted
# onto every node via extraMounts above, in place from the moment each node
# container started.
#
# The EXIT trap above is disarmed here, on success, rather than left to fire
# when this script itself exits: a docker bind mount is a live view of the
# source path, not a one-time copy, so deleting REGISTRY_CERTS_DIR after
# "kind create cluster" returns empties the very mount every later pull on
# this cluster depends on (confirmed locally: killing this script after a
# successful create, which runs the trap on the way out, left
# /etc/containerd/certs.d empty on the running node). Each slot creates
# exactly one cluster for its whole lifetime (run_shard.sh's run_slot, not
# once per attempt), and the cluster outlives this script; nothing else ever
# deletes it mid-run, so nothing else is a correct place to remove this
# directory either. It is reclaimed anyway when the box terminates at the
# end of the shard, the same way the generated MIRRORED_CONFIG file already
# is.
if [ -n "${REGISTRY_CERTS_DIR:-}" ]; then
    trap - EXIT
fi

echo "==> Step 2: Install Calico CNI"
# Vendored, not fetched: a benchmark run must not depend on GitHub being
# reachable mid-run. See sregym/resources/vendored/README.md for the
# refresh story; this file's own version must match CALICO_VERSION above.
kubectl apply -f "$SCRIPT_DIR/manifests/calico-${CALICO_VERSION}.yaml"

echo "==> Step 3: Wait for Calico to be ready"
# 300s (vs 120s) accommodates first-time image pulls on slow CI runners,
# where 1 of N calico-node pods routinely lags behind the others.
CALICO_TIMEOUT="${CALICO_TIMEOUT:-300s}"

dump_calico_diagnostics() {
    echo ""
    echo "❌ Calico did not reach Ready within ${CALICO_TIMEOUT}. Dumping diagnostics:"
    echo "--- nodes ---"
    kubectl get nodes -o wide || true
    echo "--- calico pods ---"
    kubectl -n kube-system get pods -l k8s-app=calico-node -o wide || true
    echo "--- describe unready calico pods ---"
    kubectl -n kube-system get pods -l k8s-app=calico-node \
        --field-selector=status.phase!=Running -o name 2>/dev/null \
        | xargs -r -I{} kubectl -n kube-system describe {} || true
    echo "--- recent kube-system events ---"
    kubectl -n kube-system get events --sort-by='.lastTimestamp' | tail -40 || true
}

if ! kubectl rollout status daemonset/calico-node -n kube-system --timeout="${CALICO_TIMEOUT}"; then
    dump_calico_diagnostics
    exit 1
fi
if ! kubectl wait --for=condition=ready pod -l k8s-app=calico-node -n kube-system --timeout="${CALICO_TIMEOUT}"; then
    dump_calico_diagnostics
    exit 1
fi

echo "==> Step 3b: Confirm all nodes are Ready"
# Calico rollout completing does not on its own guarantee nodes flip to Ready
# (kubelet has its own debounce). Assert it explicitly so a stuck node fails
# loudly here, not 10 minutes later inside wait_for_ready('kube-system').
if ! kubectl wait --for=condition=Ready nodes --all --timeout=120s; then
    echo "❌ Nodes did not reach Ready after Calico install."
    kubectl get nodes -o wide || true
    kubectl describe nodes || true
    exit 1
fi

echo "==> Step 4: Delete SREGym cluster baseline cache"
# SREGym caches the cluster baseline state after first deployment.
# Deleting it forces SREGym to capture a fresh baseline with Calico installed.
# Honours SREGYM_CACHE_DIR (see sregym/paths.py) so clearing one run's baseline
# does not clear another's.
rm -f "${SREGYM_CACHE_DIR:-$HOME/cache_dir}/cluster_baseline_state.json"

echo ""
echo "✅ Cluster setup complete!"
echo ""
