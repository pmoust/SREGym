import os
from pathlib import Path

HOME_DIR = Path(os.path.expanduser("~"))
BASE_DIR = Path(__file__).resolve().parent
BASE_PARENT_DIR = Path(__file__).resolve().parent.parent

# Targe microservice and its utilities directories
TARGET_MICROSERVICES = BASE_PARENT_DIR / "SREGym-applications"

# Cache directories. SREGYM_CACHE_DIR gives each run sharing a host its own,
# because what is cached below is per-cluster: a second run that reads the first
# run's cluster_baseline_state.json compares against a baseline captured from a
# different cluster, and does it silently.
CACHE_DIR = Path(os.getenv("SREGYM_CACHE_DIR") or HOME_DIR / "cache_dir")
LLM_CACHE_FILE = CACHE_DIR / "llm_cache.json"

# Cluster baseline state snapshot (captured from a fresh cluster)
CLUSTER_BASELINE_STATE_FILE = CACHE_DIR / "cluster_baseline_state.json"


def real_kubeconfig_path() -> str:
    """Path to the unproxied kubeconfig for the cluster this run owns.

    Three callers need the real cluster rather than the filtering proxy: the
    proxy itself resolving its upstream, the container runner mounting a
    bypass for in-container oracles, and those oracles reading it. All three
    used to hardcode ~/.kube/config, which breaks as soon as runs share a host,
    because each run's cluster lives in its own kubeconfig and ~/.kube/config
    then holds another run's cluster or nothing at all. Resolved at call time,
    not import time, so it reflects the environment the process was given.

    SREGYM_REAL_KUBECONFIG wins because inside an agent container KUBECONFIG
    deliberately points at the proxy, and that variable is the mounted bypass.
    """
    return os.getenv("SREGYM_REAL_KUBECONFIG") or os.getenv("KUBECONFIG") or str(HOME_DIR / ".kube" / "config")


# Fault scripts
FAULT_SCRIPTS = BASE_DIR / "generators" / "fault" / "script"

# Metadata files
SOCIAL_NETWORK_METADATA = BASE_DIR / "service" / "metadata" / "social-network.json"
HOTEL_RES_METADATA = BASE_DIR / "service" / "metadata" / "hotel-reservation.json"
PROMETHEUS_METADATA = BASE_DIR / "service" / "metadata" / "prometheus.json"
LOKI_METADATA = BASE_DIR / "service" / "metadata" / "loki.json"
TRAIN_TICKET_METADATA = BASE_DIR / "service" / "metadata" / "train-ticket.json"
ASTRONOMY_SHOP_METADATA = BASE_DIR / "service" / "metadata" / "astronomy-shop.json"
TIDB_METADATA = BASE_DIR / "service" / "metadata" / "tidb-with-operator.json"
FLIGHT_TICKET_METADATA = BASE_DIR / "service" / "metadata" / "flight-ticket.json"
FLEET_CAST_METADATA = BASE_DIR / "service" / "metadata" / "fleet-cast.json"
BLUEPRINT_HOTEL_RES_METADATA = BASE_DIR / "service" / "metadata" / "blueprint-hotel-reservation.json"

# Khaos DaemonSet
KHAOS_DS = BASE_DIR / "service" / "khaos.yaml"

# MCP Server
MCP_SERVER_K8S = BASE_PARENT_DIR / "mcp_server" / "k8s"
