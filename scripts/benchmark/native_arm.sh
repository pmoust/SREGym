#!/usr/bin/env bash
# The cluster-side half of the native arm (SREGYM_ARM=native).
#
# Five subcommands, all no-ops for the MCP arm because nothing calls them
# there. They live in one script rather than five so run_shard.sh, which is
# already long and is sourced into the bootstrap's shell, gains five call
# sites and no new logic.
#
#   kind-config <arch> <private-ip>
#       Print the path to a kind cluster config with the API server bound to
#       the host's private address instead of loopback.
#
#   kubeconfig <namespace> <out-path>
#       Create the read-only ServiceAccount brain-go's Kubernetes connection
#       authenticates as, and write a kubeconfig for it.
#
#   forwards <prom-port> <loki-port> <jaeger-port> <log-path>
#       Keep host port-forwards alive to the three in-cluster telemetry
#       backends. Prints the keeper's pid.
#
#   install-edge <cluster-name> <graph-url> <api-key> <namespace>
#       Install the edge agent into the slot's cluster.
#
#   edge-versions
#       Print "image<TAB>chart" for the manifest.
#
#   private-ip
#       Print the host's RFC1918 address, the one every address in this arm is
#       built from.
#
#   fetch-chart
#       Download the packaged edge chart once and print its path.
#
# Tracked by NOFireAI/brain-go#2410.

set -uo pipefail

# The edge agent's published image and chart. Pinned rather than floating:
# an arm comparison that cannot say which agent produced its graph is not a
# comparison. The image is a release tag, not :latest, so a new edge release
# cannot change the agent between two runs of one measurement. v3.7.0 had the
# same digest as :latest when the pin was made (sha256:d25f2e3e...).
# Overridable so a run can test an unreleased build.
EDGE_IMAGE="${SREGYM_EDGE_IMAGE:-nofireai/edge:v3.7.0}"
EDGE_CHART_REPO="${SREGYM_EDGE_CHART_REPO:-NOFireAI/edge-helm-chart}"
EDGE_CHART_VERSION="${SREGYM_EDGE_CHART_VERSION:-0.7.0}"

# The resources the agent runs an informer for (config.kube.resources).
#
# The chart's list replaces the agent's compiled default rather than adding to
# it. A Helm values override of a list replaces the whole list too, so this
# repeats chart 0.7.0's 16 entries in the chart's order (values.yaml at tag
# nofire-edge-0.7.0) and adds more after them. "services" is not a name the
# agent knows ("k8services" is its Service informer); it is kept because the
# chart sends it and dropping it is not this change. endpoints is in the
# chart's list and its ClusterRole as of 0.7.0, so it needs no rule here.
#
# The next five are informers edge v3.7.0 has that the chart's list leaves
# out. networkpolicies, poddisruptionbudgets and resourcequotas need no rule
# (the chart's ClusterRole grants them); horizontalpodautoscalers and
# storageclasses get theirs in edge_values. An HPA whose ScalingActive
# condition turns false, or a quota a fault tightens, is a change the graph
# now records.
#
# The last nine are the chart's own opt-in kinds, listed there commented out:
# cronjobs, priorityclasses, roles, clusterroles, rolebindings,
# clusterrolebindings and the three Calico kinds. The chart's ClusterRole
# already grants them, so they need no rule. The agent lists a resource before
# starting its informer and skips it when the list is refused or the CRD is
# absent, so the Calico kinds cost nothing on a cluster without Calico. They
# are on because cluster-wide objects sit behind preemption, permission and
# pod-network faults, and with them on those objects and their change events
# reach the entity graph.
EDGE_KUBE_RESOURCES="pods nodes k8services endpoints configmaps secrets persistentvolumeclaims persistentvolumes deployments replicasets statefulsets daemonsets jobs ingresses namespaces services networkpolicies poddisruptionbudgets horizontalpodautoscalers resourcequotas storageclasses cronjobs priorityclasses roles clusterroles rolebindings clusterrolebindings calicoippools calicobgppeers calicoipamconfigs"

# How often the agent's informers resync and how often it publishes the graph,
# both under the chart's 5m defaults.
#
# The publish interval is 60s because that is the agent's floor, not a choice.
# It exits at startup on anything shorter ("the interval to publish the graph
# must be greater or equal to 1 minute", edge internal/edge/agent/agent.go).
# run-20260928-1641-d4349432 set 30s: the agent crash-looped for the whole run
# and every attempt failed the empty-graph gate.
#
# The publisher waits one interval, publishes if the graph changed, then waits
# again. Ticks are therefore 60s plus one publish apart: about 60s, 120s, 180s
# after the agent starts when a publish takes seconds, and never more than 90s
# apart because a publish times out at 30s.
#
# Before the fault: run_shard.sh sets SREGYM_BASELINE_SYNC_SECONDS=150, which
# the conductor sleeps after the application is deployed. A 150s span holds
# at least two ticks when a publish takes under 15s (150 / 75), and at least
# one at the 30s timeout (150 / 90). So the application is published inside
# the window, and the last publish before injection is at most one tick old.
# That publish is the before-state change detection diffs the fault against.
#
# After the fault: the first publish lands 0s to 90s after injection. The
# agent has ticked since install, so injection falls at a random point in its
# cycle. The conductor holds diagnosis until brain-go shows a publish from
# after the fault, capped at 120s (one tick plus the publish timeout, with
# margin; native_gate.wait_for_fault_publish). Reaching the cap does not fail
# the attempt, because a fault that changes no watched object produces no
# visible publish; arm-native.json records it as "cap".
#
# The resync interval has no validation in the agent (it goes straight to the
# informer factory). A resync replays the informers' local cache to their
# handlers and does not re-list from the API server; changes arrive through
# the watch either way. 30s is kept so a missed watch event is replayed well
# inside one publish interval.
EDGE_RESYNC_INTERVAL="${SREGYM_EDGE_RESYNC_INTERVAL:-30s}"
EDGE_GRAPH_INTERVAL="${SREGYM_EDGE_GRAPH_INTERVAL:-60s}"

# How long install-edge waits for the agent to prove itself after Helm.
#
# Ready: 180s for the pod to be Ready with no restarts. Helm's wait can return
# before the image is even pulled (see install-edge), and each kind node pulls
# the image from Docker Hub itself.
#
# Publish: 150s for brain-go to hold at least one entity for the account. The
# first two publishes land about 60s and 120s after the agent starts, and each
# may take up to its own 30s timeout, so 150s covers both.
EDGE_READY_SECONDS="${SREGYM_EDGE_READY_SECONDS:-180}"
EDGE_PUBLISH_SECONDS="${SREGYM_EDGE_PUBLISH_SECONDS:-150}"
EDGE_POLL_SECONDS="${SREGYM_EDGE_POLL_SECONDS:-5}"

# The Secret the chart reads the API key from. Named by the chart's own
# default (config.publisher.apiKeySecret.name).
EDGE_SECRET_NAME="nofire-credentials"

# How often the keeper checks the telemetry forwards.
FORWARD_CHECK_SECONDS="${SREGYM_FORWARD_CHECK_SECONDS:-10}"

log() { echo "[native-arm] $*" >&2; }
die() { echo "[native-arm] ERROR: $*" >&2; exit 1; }

# --- private-ip --------------------------------------------------------------
# Every address this arm builds (the API server's bind address, the three
# telemetry URLs, the edge agent's publish endpoint) is this one value. It is
# resolved through here rather than repeated at each site, because three
# copies of a shell pipeline that disagree would be three components pointed
# at different hosts with no single error to read.
#
# SREGYM_PRIVATE_IP wins when set, so a box whose first global address is not
# the reachable one can be corrected in one place.
cmd_private_ip() {
  local ip
  ip="${SREGYM_PRIVATE_IP:-$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)}"
  [ -n "$ip" ] || die "could not determine a private IPv4 address; set SREGYM_PRIVATE_IP"

  # RFC1918 checked, not assumed. brain-go's guard is not the reason:
  # allow_private_networks lifts its whole non-public address check, so
  # loopback, link-local (169.254.169.254 included), CGNAT and ULA would all
  # be dialable, and a public address needs no opt-in at all. The check exists
  # only because this arm expects the host's RFC1918 address. Loopback is the
  # one class with a concrete failure: brain-go dials from inside a container,
  # where loopback is the container's own (see kind-config). The value is also
  # exported to seed_slot.sh, which applies the same RFC1918 check, so failing
  # here reports it before any cluster is built.
  case "$ip" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) ;;
    *) die "resolved '$ip', which is not RFC1918; this arm expects the host's RFC1918 address" ;;
  esac

  echo "$ip"
}

# --- kind-config -------------------------------------------------------------
# kind binds the API server to 127.0.0.1 by default, which brain-go cannot
# reach: it dials from inside a container, where loopback is the container's
# own. Setting networking.apiServerAddress moves the published port onto the
# host's private address and puts that address in the API server certificate's
# SANs, so TLS still verifies and no kubeconfig rewriting is needed afterwards.
#
# Written as a copy rather than an edit of kind/kind-config-<arch>.yaml,
# because that file is upstream's and the MCP arm must keep using it verbatim.
cmd_kind_config() {
  local arch="$1" ip="$2"
  local src out
  src="$(cd "$(dirname "$0")/../.." && pwd)/kind/kind-config-${arch}.yaml"
  [ -f "$src" ] || die "no kind config at $src"

  out="${SREGYM_KIND_CONFIG_OUT:-/tmp/sregym-kind-${KIND_CLUSTER_NAME:-kind}.yaml}"

  # Inserted right after the networking: key the upstream file already has,
  # so there is one networking block rather than two. A second top-level
  # networking key would silently win or lose depending on the YAML parser.
  awk -v ip="$ip" '
    { print }
    /^networking:$/ { print "  apiServerAddress: \"" ip "\"" }
  ' "$src" >"$out" || die "could not write $out"

  grep -q "apiServerAddress" "$out" || die "$src has no networking: block to patch"

  echo "$out"
}

# --- kubeconfig --------------------------------------------------------------
# The credential brain-go's Kubernetes connection authenticates as.
#
# A dedicated read-only ServiceAccount, not kind's own kubeconfig. kind's
# credential is a cluster-admin client certificate; this one can read exactly
# the resources brain-go's Kubernetes tool family reads (list/get on the core
# and apps objects behind k8s_list_workloads, k8s_describe, k8s_get_events,
# k8s_rollout_*, k8s_get_nodes and k8s_namespace_summary) and nothing else.
#
# It deliberately grants nothing on chaos-mesh's CRDs. If the agent under test
# can read a PodChaos or NetworkChaos object it reads the injected fault
# verbatim, and the benchmark has leaked its own ground truth. That is the
# same rule the edge agent's ServiceAccount is held to, and it is a
# correctness rule rather than hygiene. The MCP arm's kubectl ClusterRole
# (mcp_server/k8s/clusterrole.yaml) grants no chaos-mesh CRDs either, so the
# two arms see the same cluster.
#
# The token comes from a Secret of type kubernetes.io/service-account-token,
# not from `kubectl create token`. A projected token defaults to an hour, an
# attempt can outlast that, and a token expiring mid-run looks exactly like a
# broken connection. The cluster is torn down with the slot.
cmd_kubeconfig() {
  local ns="$1" out="$2"
  local sa="sregym-brain-readonly"

  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null \
    || die "could not create namespace $ns"

  # The rules below are derived from brain-go, not guessed. They are every
  # resource reached by a clientset or dynamic-client call in
  # internal/clients/kubernetes at brain-go describe-more-kinds e744e086, plus
  # the two Gets in policy.go from brain-go fix/k8s-tool-diagnosis-fields
  # (NOFireAI/brain-go#4834), which is the whole API surface this connection
  # has. Until #4834 merges, a re-derivation on brain-go main does not return
  # networkpolicies or resourcequotas; keep both rules. The two grants are
  # unused, not wrong, against a brain-go without that change:
  # the tool adapter (internal/app/kubernetes_tool_resolver.go) calls only
  # that package's methods, and the connection probe calls its
  # IsConnectionValid.
  #
  # To re-derive after a brain-go change:
  #   git grep -n 'clientset\.\|dynamicClient\.' -- internal/clients/kubernetes
  #
  # What that returns today, by file:
  #   client.go     CoreV1 Namespaces List          (the connection probe)
  #   namespace.go  CoreV1 Pods List
  #   nodes.go      CoreV1 Nodes List
  #   events.go     CoreV1 Events List
  #   workloads.go  AppsV1 Deployments/StatefulSets/DaemonSets List,
  #                 BatchV1 Jobs List
  #   rollout.go    AppsV1 Deployments/StatefulSets/DaemonSets Get,
  #                 AppsV1 ReplicaSets/ControllerRevisions List
  #   describe.go   AppsV1 Deployments/StatefulSets/DaemonSets/ReplicaSets Get,
  #                 BatchV1 Jobs Get, CoreV1 Pods/Nodes/Namespaces/Services/
  #                 ConfigMaps/PersistentVolumeClaims Get,
  #                 AutoscalingV2 HorizontalPodAutoscalers Get,
  #                 SchedulingV1 PriorityClasses Get, BatchV1 CronJobs Get,
  #                 RbacV1 Roles/ClusterRoles/RoleBindings/
  #                 ClusterRoleBindings Get, CoreV1 ServiceAccounts Get
  #   calico.go     dynamic crd.projectcalico.org/v1 ippools/bgppeers/
  #                 ipamconfigs Get
  #   policy.go     NetworkingV1 NetworkPolicies Get,
  #                 CoreV1 ResourceQuotas Get
  #
  # The kinds k8s_describe added after e49e7711 are reached only by a Get, so
  # their rules grant get and nothing else. Two of those added kinds,
  # priorityclasses and serviceaccounts, have no counterpart in the MCP arm's
  # kubectl ClusterRole (mcp_server/k8s/clusterrole.yaml), so on a problem
  # that needs one of those objects the native arm can read it and the MCP arm
  # cannot. The MCP arm is left as it is: it is the anchor archived runs were
  # measured against. A ServiceAccount describe shows a secret count, never a
  # secret name, and the role still grants no secrets.
  #
  # NetworkPolicy and ResourceQuota (brain-go fix/k8s-tool-diagnosis-fields)
  # are also reached only by a Get. Their spec and limits are what Sonnet 5.5
  # reported it could not read on the network-policy and
  # namespace-memory-limit problems. The MCP arm already grants both.
  #
  # batch/jobs is not optional and a missing grant is not a partial outage.
  # ListWorkloads lists jobs last and returns (nil, err) on failure, so a 403
  # there discards the deployments, statefulsets and daemonsets it had already
  # collected: k8s_list_workloads, one of the tools this arm exists to
  # exercise, would fail on every call in every namespace whether or not any
  # Job exists.
  #
  # Deliberately absent: events.k8s.io, because CoreV1().Events() is served
  # by the core group and nothing here touches the newer API; and list on
  # cronjobs, because ListWorkloads lists four kinds, none of them CronJob.
  #
  # No chaos-mesh, which is the correctness rule rather than the least
  # privilege one: an agent that can read a PodChaos reads the injected fault
  # verbatim. Asserted below rather than trusted.
  kubectl apply -f - >/dev/null <<YAML || die "could not apply the read-only ServiceAccount"
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${sa}
  namespace: ${ns}
---
apiVersion: v1
kind: Secret
metadata:
  name: ${sa}-token
  namespace: ${ns}
  annotations:
    kubernetes.io/service-account.name: ${sa}
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${sa}
rules:
- apiGroups: [""]
  resources: ["configmaps", "events", "namespaces", "nodes", "persistentvolumeclaims", "pods", "services"]
  verbs: ["get", "list"]
- apiGroups: ["apps"]
  resources: ["controllerrevisions", "daemonsets", "deployments", "replicasets", "statefulsets"]
  verbs: ["get", "list"]
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["get", "list"]
- apiGroups: ["batch"]
  resources: ["cronjobs"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["serviceaccounts"]
  verbs: ["get"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get"]
- apiGroups: ["scheduling.k8s.io"]
  resources: ["priorityclasses"]
  verbs: ["get"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "clusterroles", "rolebindings", "clusterrolebindings"]
  verbs: ["get"]
- apiGroups: ["crd.projectcalico.org"]
  resources: ["ippools", "bgppeers", "ipamconfigs"]
  verbs: ["get"]
- apiGroups: ["networking.k8s.io"]
  resources: ["networkpolicies"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["resourcequotas"]
  verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${sa}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${sa}
subjects:
- kind: ServiceAccount
  name: ${sa}
  namespace: ${ns}
YAML

  # The token controller fills the Secret asynchronously, so an immediate read
  # returns an empty data map rather than failing.
  local token="" deadline
  deadline=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    token="$(kubectl -n "$ns" get secret "${sa}-token" -o jsonpath='{.data.token}' 2>/dev/null)"
    [ -n "$token" ] && break
    sleep 2
  done
  [ -n "$token" ] || die "the ServiceAccount token Secret never filled in"
  token="$(printf '%s' "$token" | base64 -d)"

  # Taken from the slot's own kubeconfig so the address and CA are whatever
  # kind actually chose, rather than a second guess at them here.
  local server ca
  server="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')"
  ca="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
  [ -n "$server" ] || die "could not read the API server address from the slot's kubeconfig"
  [ -n "$ca" ] || die "could not read the cluster CA from the slot's kubeconfig"

  case "$server" in
    https://127.0.0.1:*|https://localhost:*)
      die "the API server is on loopback ($server); the cluster was not created with an apiServerAddress and brain-go cannot reach it"
      ;;
  esac

  umask 077
  cat >"$out" <<YAML || die "could not write $out"
apiVersion: v1
kind: Config
clusters:
- name: sregym
  cluster:
    server: ${server}
    certificate-authority-data: ${ca}
users:
- name: ${sa}
  user:
    token: ${token}
contexts:
- name: sregym
  context:
    cluster: sregym
    user: ${sa}
current-context: sregym
YAML

  # The ground-truth check, run rather than assumed. A ClusterRole that
  # somehow granted these would hand the injected fault to the agent under
  # test and every score after it would be meaningless.
  local leaked=""
  for crd in podchaos.chaos-mesh.org networkchaos.chaos-mesh.org stresschaos.chaos-mesh.org; do
    if [ "$(kubectl auth can-i list "$crd" -A --as "system:serviceaccount:${ns}:${sa}" 2>/dev/null)" = "yes" ]; then
      leaked="$leaked $crd"
    fi
  done
  [ -z "$leaked" ] || die "the read-only ServiceAccount can list${leaked}; that is the benchmark's own ground truth"

  log "read-only kubeconfig written for ${ns}/${sa} against ${server}"
}

# --- forwards ----------------------------------------------------------------
# Host port-forwards to the three in-cluster telemetry backends, the same ones
# the MCP arm's endpoints proxy to: prometheus-server, loki and jaeger-out in
# the observe namespace.
#
# Supervised rather than started once. Those three services belong to the
# application, which the conductor deploys and tears down per problem, so a
# forward established at slot start is dead by the second problem. The MCP
# server does not have this property: it lives in its own namespace and is
# deployed once per slot.
#
# A forward being down between problems is expected and not worth logging as
# a failure; nothing queries then. What matters is that one is up by the time
# the pre-fault window opens, which the keeper's interval guarantees and the
# entity-count gate proves.
cmd_forwards() {
  local prom="$1" loki="$2" jaeger="$3" logfile="$4"

  # The keeper's own stdout goes to the log, not to this function's. The caller
  # reads the pid with a command substitution, which blocks until every writer
  # to that pipe closes it, and a backgrounded subshell that inherited stdout
  # never does: the caller would hang for the life of the slot.
  (
    prom_pid=""; loki_pid=""; jaeger_pid=""
    while true; do
      for spec in "prometheus-server:80:$prom:prom_pid" "loki:3100:$loki:loki_pid" "jaeger-out:16686:$jaeger:jaeger_pid"; do
        svc="${spec%%:*}"; rest="${spec#*:}"
        target="${rest%%:*}"; rest="${rest#*:}"
        port="${rest%%:*}"; var="${rest#*:}"
        eval "pid=\$$var"

        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
          continue
        fi

        # Bound to 0.0.0.0 for the same reason the MCP forward is: brain-go
        # dials from inside a container and reaches the host by its private
        # address, not by loopback.
        nohup kubectl port-forward "svc/$svc" "$port:$target" -n observe --address 0.0.0.0 \
          >>"$logfile" 2>&1 &
        eval "$var=\$!"
      done
      sleep "$FORWARD_CHECK_SECONDS"
    done
  ) >>"$logfile" 2>&1 &

  echo "$!"
}

# --- install-edge ------------------------------------------------------------
cmd_install_edge() {
  local cluster="$1" graph_url="$2" api_key="$3" ns="$4"

  # The chart the box already downloaded. Not fetched here: this runs inside a
  # slot's background subshell alongside its siblings, and concurrent
  # downloads to one path are what the fetch-chart comment above describes.
  local chart="${SREGYM_EDGE_CHART:-}"
  [ -n "$chart" ] || die "SREGYM_EDGE_CHART is unset; run_shard.sh fetches the chart once per box"
  [ -s "$chart" ] || die "no edge chart at $chart"

  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null \
    || die "could not create namespace $ns"

  # The key goes into a Secret the chart reads through NOFIRE_API_KEY, not
  # into config.publisher.apiKey. That value is rendered into the ConfigMap in
  # plaintext, and a ConfigMap is one of the objects brain-go's own
  # k8s_describe tool will return, so the agent under test could read its
  # account's edge credential out of the cluster it is investigating. The
  # Kubernetes client never builds a Secrets client and refuses "secret"
  # before its describe allowlist, so a Secret is out of reach.
  kubectl -n "$ns" create secret generic "$EDGE_SECRET_NAME" \
    --from-literal=api-key="$api_key" --dry-run=client -o yaml | kubectl apply -f - >/dev/null \
    || die "could not create the edge API key Secret"

  # The informer list and the extra RBAC rules go in a values file rather than
  # --set: a list of 30 strings and nested RBAC rules are unreadable as --set
  # paths, and --set still overrides the file for any key both carry.
  local values
  values="$(mktemp "${TMPDIR:-/tmp}/nofire-edge-values.XXXXXX")" || die "could not create the edge values file"
  # shellcheck disable=SC2064  # expand now: values is local and gone at exit
  trap "rm -f '$values'" EXIT
  edge_values >"$values" || die "could not write the edge values file"

  # Every disabled feature is disabled explicitly, not left to the chart's
  # default, so a chart bump cannot quietly turn one on in a benchmark run:
  #
  #   enableCausalAnalysis  the agent's own causal ranking. The benchmark
  #                         grades brain-go's diagnosis, and a second ranker
  #                         running in the cluster is a confound.
  #   netobs                L7 dependency detection through Hubble, Istio or
  #                         Caretta. None is installed here, so it would
  #                         auto-detect nothing every interval.
  #   sentry.dsn            the chart ships NOFire's production DSN. Sixty
  #                         ephemeral clusters reporting into the product's
  #                         own Sentry project is noise in the wrong place.
  #   configMapCapture      the agent captures ConfigMap values for change
  #   .clearText            detection. false keeps them hashed, never in
  #                         cleartext in the graph the agent under test reads.
  #   envCapture.clearText  the same for container environment values.
  #
  # --wait catches a release Helm cannot apply at all, and nothing more on
  # this chart. Chart 0.7.0 runs one replica with maxUnavailable: 1, and Helm
  # 3's waiter (what the bootstrap installs) counts a Deployment ready at
  # replicas - maxUnavailable = 1 - 1 = 0 ready pods (pkg/kube/ready.go). A
  # crash-looping agent passes it, which is how run-20260928-1641-d4349432
  # lost every attempt. verify_edge below is the check that means something.
  helm upgrade --install nofire-edge "$chart" \
    --namespace "$ns" \
    -f "$values" \
    --set "image.repository=${EDGE_IMAGE%:*}" \
    --set "image.tag=${EDGE_IMAGE##*:}" \
    --set "config.kube.clusterName=$cluster" \
    --set "config.kube.resyncInterval=$EDGE_RESYNC_INTERVAL" \
    --set "config.publisher.graph.url=$graph_url" \
    --set "config.publisher.graph.interval=$EDGE_GRAPH_INTERVAL" \
    --set config.publisher.apiKeySecret.enabled=true \
    --set "config.publisher.apiKeySecret.name=$EDGE_SECRET_NAME" \
    --set config.enablePublishing=true \
    --set config.enableCausalAnalysis=false \
    --set config.netobs.enabled=false \
    --set config.sentry.dsn= \
    --set config.configMapCapture.clearText=false \
    --set config.envCapture.clearText=false \
    --set edgeProxy.enabled=false \
    --wait --timeout 5m >&2 \
    || die "helm install of the edge agent failed"

  # Same ground-truth rule as the read-only ServiceAccount above, applied to
  # the agent that builds the entity graph. The chart's ClusterRole grants no
  # chaos-mesh CRDs; this is what proves it on the cluster that will be
  # graded, rather than in the chart that was read.
  local edge_sa
  edge_sa="$(kubectl -n "$ns" get deploy nofire-edge -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null)"
  [ -n "$edge_sa" ] || die "the edge deployment names no ServiceAccount"

  if [ "$(kubectl auth can-i list podchaos.chaos-mesh.org -A --as "system:serviceaccount:${ns}:${edge_sa}" 2>/dev/null)" = "yes" ]; then
    die "the edge ServiceAccount can list chaos-mesh CRDs; the injected fault would appear in the entity graph"
  fi

  verify_edge "$ns"

  log "edge agent installed into $cluster ($EDGE_IMAGE, chart $EDGE_CHART_VERSION), publishing to $graph_url every $EDGE_GRAPH_INTERVAL"
}

# The values file install-edge hands Helm: the informer list and the extra
# RBAC rules described at EDGE_KUBE_RESOURCES. The chart renders
# rbac.extraRules with toYaml into its ClusterRole's rules list
# (templates/rbac.yaml at nofire-edge-0.7.0), so each entry is one whole rule.
edge_values() {
  local r
  echo "config:"
  echo "  kube:"
  echo "    resources:"
  for r in $EDGE_KUBE_RESOURCES; do
    echo "      - \"$r\""
  done
  cat <<'YAML'
rbac:
  extraRules:
    - apiGroups: ["autoscaling"]
      resources: ["horizontalpodautoscalers"]
      verbs: ["get", "list", "watch"]
    - apiGroups: ["storage.k8s.io"]
      resources: ["storageclasses"]
      verbs: ["get", "list", "watch"]
YAML
}

# What the slot log needs to explain an agent that never published. Written
# to stderr, which run_shard.sh sends to the slot log, which is archived to S3.
# Each command is best effort: a failed dump must not hide the die after it.
dump_edge_diagnostics() {
  local ns="$1"

  echo "[native-arm] --- kubectl -n $ns get pods -o wide" >&2
  kubectl -n "$ns" get pods -o wide >&2 2>&1 || true
  echo "[native-arm] --- kubectl -n $ns get events (last 30)" >&2
  kubectl -n "$ns" get events --sort-by=.lastTimestamp 2>&1 | tail -30 >&2 || true
  echo "[native-arm] --- kubectl -n $ns logs deploy/nofire-edge --tail=100" >&2
  kubectl -n "$ns" logs deploy/nofire-edge --tail=100 >&2 2>&1 || true
  # A crash-looping container is often between starts, with no current log.
  # The previous container's log is where its exit message is.
  echo "[native-arm] --- kubectl -n $ns logs deploy/nofire-edge --previous --tail=100" >&2
  kubectl -n "$ns" logs deploy/nofire-edge --previous --tail=100 >&2 2>&1 || true
}

# Prove the agent runs and has published, since Helm's wait proves neither
# on this chart. The agent's /healthz returns 200 unconditionally, so pod
# readiness only says the process is up; the entity count in brain-go is the
# only evidence that a publish arrived.
#
# Two bounded polls. First the pod: Ready with restartCount 0. A restart is
# failed on at once rather than waited out, because the agent's startup
# faults (a bad config value, as in the run above) exit on every start.
# Then the graph: brain_graph.py counts the account's entities under the
# slot's session, the same reader the conductor's pre-fault gate uses.
verify_edge() {
  local ns="$1"
  local selector="app.kubernetes.io/instance=nofire-edge,app.kubernetes.io/name=nofire-edge"

  local brain_url="${BRAIN_ADMIN_API_URL:-}" session="${BRAIN_SESSION:-}"
  [ -n "$brain_url" ] || die "BRAIN_ADMIN_API_URL is unset; seed_slot.sh exports it before install-edge runs"
  [ -n "$session" ] || die "BRAIN_SESSION is unset; seed_slot.sh exports it before install-edge runs"

  local pods deadline state=""
  deadline=$(( $(date +%s) + EDGE_READY_SECONDS ))
  while :; do
    pods="$(kubectl -n "$ns" get pods -l "$selector" \
      -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].ready}{" "}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null)"
    # Every pod Ready with no restarts, and at least one pod.
    state="$(printf '%s\n' "$pods" | awk '
      NF == 0 { next }
      { n++ }
      $3 != "" && $3 != "0" { restarted = 1 }
      $2 != "true" { waiting = 1 }
      END {
        if (restarted) print "restarted"
        else if (n == 0 || waiting) print "waiting"
        else print "ready"
      }
    ')"
    [ "$state" = "ready" ] && break
    if [ "$state" = "restarted" ]; then
      dump_edge_diagnostics "$ns"
      die "the edge agent restarted after install; see its logs above"
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      dump_edge_diagnostics "$ns"
      die "the edge agent pod was not Ready within ${EDGE_READY_SECONDS}s"
    fi
    sleep "$EDGE_POLL_SECONDS"
  done
  log "edge agent pod Ready with no restarts"

  local root counts errfile last_err=""
  root="$(cd "$(dirname "$0")/../.." && pwd)"
  errfile="$(mktemp)" || die "could not create a temporary file"
  deadline=$(( $(date +%s) + EDGE_PUBLISH_SECONDS ))
  while :; do
    # Exits 3 when the account holds no entities or the read failed, and
    # prints the counts (or the read's error class) either way. Its stderr is
    # kept, not discarded: a broken uv or Python would otherwise read as an
    # agent that has not published. The session is passed as an argument and
    # never written to stderr, so the line is safe to print.
    if counts="$(uv run --project "$root" python "$root/scripts/benchmark/brain_graph.py" \
         --brain-url "$brain_url" --session "$session" --require-entities 2>"$errfile")"; then
      break
    fi
    last_err="$(tail -n 1 "$errfile" 2>/dev/null)"
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[native-arm] --- last entity count from brain-go" >&2
      printf '%s\n' "$counts" >&2
      echo "[native-arm] --- last stderr line from the entity count" >&2
      printf '%s\n' "$last_err" >&2
      rm -f "$errfile"
      dump_edge_diagnostics "$ns"
      die "brain-go held no entities for this account ${EDGE_PUBLISH_SECONDS}s after the edge agent was Ready; it has not published"
    fi
    sleep "$EDGE_POLL_SECONDS"
  done
  rm -f "$errfile"
  log "brain-go holds entities for this account; the edge agent has published"
}

# --- fetch-chart -------------------------------------------------------------
# Download the packaged chart from its GitHub release and print the local path.
# The repository is private, so this goes through the API with the clone token
# the bootstrap already read from SSM rather than through helm's own HTTP
# fetch, which has nowhere to put a credential.
#
# Called once per box, before any slot starts, for the same reason the edge
# image is pulled once per box: slots run concurrently in background
# subshells, and an unsynchronised download to one shared path lets a second
# slot hand helm a file the first is still writing. That is a non-deterministic
# install failure, which is the worst kind to debug on an ephemeral box.
#
# Written to a per-process path and renamed, so even a caller that ignores the
# once-per-box rule publishes the path atomically rather than in pieces. An
# existing non-empty file is reused and not re-downloaded.
cmd_fetch_chart() {
  local tag="nofire-edge-${EDGE_CHART_VERSION}"
  local out="/tmp/${tag}.tgz"

  [ -s "$out" ] && { echo "$out"; return 0; }
  [ -n "${GH_TOKEN:-}" ] || die "GH_TOKEN is unset; the edge chart cannot be fetched"

  local asset_id
  asset_id="$(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/${EDGE_CHART_REPO}/releases/tags/${tag}" 2>/dev/null \
    | jq -r --arg name "${tag}.tgz" '.assets[]? | select(.name == $name) | .id')"

  [ -n "$asset_id" ] && [ "$asset_id" != "null" ] \
    || die "no ${tag}.tgz asset on ${EDGE_CHART_REPO} release ${tag}"

  local tmp="${out}.$$"
  curl -fsSL -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/octet-stream" \
    -o "$tmp" "https://api.github.com/repos/${EDGE_CHART_REPO}/releases/assets/${asset_id}" \
    || { rm -f "$tmp"; die "could not download the chart asset"; }

  mv -f "$tmp" "$out" || { rm -f "$tmp"; die "could not move the chart into place"; }

  echo "$out"
}

# --- edge-versions -----------------------------------------------------------
cmd_edge_versions() {
  printf '%s\t%s\n' "$EDGE_IMAGE" "$EDGE_CHART_VERSION"
}

case "${1:-}" in
  kind-config)  shift; cmd_kind_config "$@" ;;
  kubeconfig)   shift; cmd_kubeconfig "$@" ;;
  forwards)     shift; cmd_forwards "$@" ;;
  install-edge) shift; cmd_install_edge "$@" ;;
  edge-versions) cmd_edge_versions ;;
  private-ip)   cmd_private_ip ;;
  fetch-chart)  cmd_fetch_chart ;;
  *) die "usage: native_arm.sh {kind-config|kubeconfig|forwards|install-edge|edge-versions|private-ip|fetch-chart} ..." ;;
esac
