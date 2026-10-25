"""Named sets of registered SREGym problems."""

SREGYM_LITE_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "edge_request_filter_cpu_saturation",
    "network_policy_block",
    "env_variable_shadowing_astronomy_shop",
    "mutating_webhook_resource_limits_social_network",
    "finalizer_deadlock_controller_hotel_reservation",
    "kafka_poison_pill_hol_block",
    "internal_traffic_policy_local_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "namespace_memory_limit",
    "valkey_auth_disruption",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "unschedulable_incorrect_port_assignment",
    "readiness_probe_misconfiguration_social_network",
    "duplicate_pvc_mounts_social_network",
    "admission_webhook_outage_hotel_reservation",
    "wrong_dns_policy_astronomy_shop",
    "wrong_service_selector_social_network",
    "rolling_update_misconfigured_social_network",
    "search_rate_retry_collapse_hotel_reservation",
)

# Every id sregym/conductor/problems/registry.py's ProblemRegistry registers,
# copied out as pure data. It has to be pure data rather than derived from
# ProblemRegistry() itself: that class's __init__ unconditionally constructs
# KubeCtl(), which calls config.load_kube_config() and exit(1)s the whole
# process when no kubeconfig exists (sregym/service/kubectl.py). run_shard.sh
# needs this list before the instance's cluster exists, so anything that can
# fail that way cannot be on the path.
#
# tests/benchmark/test_bootstrap_manifest.py pins this tuple against the
# registry's own dict literal, read with ast rather than constructed, so the
# two cannot silently drift apart.
ALL_PROBLEM_IDS = (
    "faulty_image_correlated",
    "update_incompatible_correlated",
    "incorrect_image",
    "incorrect_port_assignment",
    "kafka_producer_leak",
    "unschedulable_incorrect_port_assignment",
    "misconfig_app_hotel_res",
    "missing_env_variable_astronomy_shop",
    "revoke_auth_mongodb-1",
    "revoke_auth_mongodb-2",
    "storage_user_unregistered-1",
    "storage_user_unregistered-2",
    "valkey_auth_disruption",
    "valkey_memory_disruption",
    "edge_request_filter_cpu_saturation",
    "ephemeral_port_range_hotel_reservation",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "capacity_decrease_rpc_retry_storm",
    "gc_capacity_degradation",
    "load_spike_rpc_retry_storm",
    "search_rate_retry_collapse_hotel_reservation",
    "assign_to_non_existent_node",
    "auth_miss_mongodb",
    "configmap_drift_hotel_reservation",
    "cfs_cpu_throttling_hotel_reservation",
    "feature_flag_latent_bug_hotel_reservation",
    "finalizer_deadlock_controller_hotel_reservation",
    "duplicate_pvc_mounts_astronomy_shop",
    "duplicate_pvc_mounts_hotel_reservation",
    "duplicate_pvc_mounts_social_network",
    "env_variable_shadowing_astronomy_shop",
    "file_descriptor_exhaustion",
    "k8s_target_port-misconfig",
    "liveness_probe_misconfiguration_astronomy_shop",
    "liveness_probe_misconfiguration_hotel_reservation",
    "liveness_probe_misconfiguration_social_network",
    "liveness_probe_too_aggressive_astronomy_shop",
    "liveness_probe_too_aggressive_hotel_reservation",
    "liveness_probe_too_aggressive_social_network",
    "init_container_dependency_hang_hotel_reservation",
    "init_container_dependency_hang_social_network",
    "init_container_dependency_hang_astronomy_shop",
    "missing_configmap_hotel_reservation",
    "missing_configmap_social_network",
    "missing_service_astronomy_shop",
    "missing_service_hotel_reservation",
    "missing_service_social_network",
    "namespace_memory_limit",
    "nightly_rebalance_oom_hotel_reservation",
    "node_clock_drift_hotel_reservation",
    "pod_anti_affinity_deadlock",
    "postgres_lock_contention_product_catalog",
    "persistent_volume_affinity_violation",
    "priority_preemption_cascade_hotel_reservation",
    "pvc_claim_mismatch",
    "rbac_misconfiguration",
    "readiness_probe_misconfiguration_astronomy_shop",
    "readiness_probe_misconfiguration_hotel_reservation",
    "readiness_probe_misconfiguration_social_network",
    "resource_request_too_large",
    "resource_request_too_small",
    "hpa_missing_effective_cpu_request_hotel_reservation",
    "rolling_update_misconfigured_hotel_reservation",
    "rolling_update_misconfigured_social_network",
    "scale_pod_zero_social_net",
    "service_dns_resolution_failure_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "sidecar_port_conflict_astronomy_shop",
    "sidecar_port_conflict_hotel_reservation",
    "sidecar_port_conflict_social_network",
    "service_port_conflict_astronomy_shop",
    "service_port_conflict_hotel_reservation",
    "service_port_conflict_social_network",
    "stale_coredns_config_astronomy_shop",
    "stale_coredns_config_social_network",
    "taint_no_toleration_social_network",
    "wrong_bin_usage",
    "wrong_dns_policy_astronomy_shop",
    "wrong_dns_policy_hotel_reservation",
    "wrong_dns_policy_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "wrong_service_selector_astronomy_shop",
    "wrong_service_selector_hotel_reservation",
    "wrong_service_selector_social_network",
    "astronomy_shop_ad_service_failure",
    "astronomy_shop_ad_service_high_cpu",
    "astronomy_shop_ad_service_image_slow_load",
    "astronomy_shop_ad_service_manual_gc",
    "astronomy_shop_cart_service_failure",
    "astronomy_shop_failed_readiness_probe",
    "astronomy_shop_payment_service_failure",
    "astronomy_shop_payment_service_unreachable",
    "astronomy_shop_product_catalog_service_failure",
    "kafka_queue_problems",
    "kafka_poison_pill_hol_block",
    "loadgenerator_flood_homepage",
    "trainticket_f17_nested_sql_select_clause_error",
    "trainticket_f22_sql_column_name_mismatch_error",
    "silent_data_corruption",
    "latent_sector_error",
    "expired_tls_hotel_reservation",
    "ingress_misroute",
    "network_policy_block",
    "node_conntrack_exhaustion_hotel_reservation",
    "dev_shm_exhaustion_hotel_reservation",
    "internal_traffic_policy_local_astronomy_shop",
    "admission_webhook_outage_hotel_reservation",
    "pod_cidr_exhaustion_hotel_reservation",
    "calico_route_reflector_label_drift_hotel_reservation",
    "admission_webhook_tls_mismatch_hotel_reservation",
    "mutating_webhook_resource_limits_social_network",
    "psa_restricted_blocks_recreation_hotel_reservation",
    "cumulative_admission_webhook_timeout_hotel_reservation",
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "kubelet_crash",
    "kubelet_eviction_threshold_misconfig",
    "workload_imbalance",
    "operator_overload_replicas",
    "operator_non_existent_storage",
    "operator_invalid_affinity_toleration",
    "operator_security_context_fault",
    "operator_wrong_update_strategy_fault",
    "operator_wrong_operator_image",
)


# Problems the benchmark harness cannot run today, each with the reason. A
# problem listed here stays in sregym-full (the registry's own list) and is
# left out of sregym-full-runnable. The reasons were read from the problem
# classes, conductor.py and scripts/benchmark/image_cache.list; none was
# confirmed by running the problem on a benchmark box.
#
# The registry mirror is fail-closed: a kind node pulls only what
# image_cache.list holds, so an image missing there is an ImagePullBackOff,
# and a deploy that cannot finish is retried 3 times per attempt. One such
# problem costs the slot that owns it far more than its own share of the wave.
# Adding the image to image_cache.list and re-seeding the cache bucket lifts
# the image-only exclusions below.
NOT_RUNNABLE_ON_BENCHMARK_BOX = {
    # Khaos needs eBPF on the node. Conductor.start_problem returns
    # SKIPPED_KHAOS_REQUIRED on any emulated (kind) cluster, which is every
    # benchmark box, so the attempt never starts.
    "silent_data_corruption": "requires Khaos (dm-flakey); skipped on a kind cluster",
    "latent_sector_error": "requires Khaos (eBPF); skipped on a kind cluster",
    # Same Khaos skip, and the registry also raises for this id on a kind
    # cluster (ProblemRegistry.non_emulated_cluster_problems).
    "node_clock_drift_hotel_reservation": "requires Khaos and a non-emulated cluster; the registry refuses it on kind",
    # The Blueprint hotel service images are in image_cache.list, but the
    # workload generator these problems start is 777lefty/wlgen-proc:latest,
    # which is not. Without it the load never starts.
    "capacity_decrease_rpc_retry_storm": "workload image 777lefty/wlgen-proc:latest is not in image_cache.list",
    "gc_capacity_degradation": "workload image 777lefty/wlgen-proc:latest is not in image_cache.list",
    "load_spike_rpc_retry_storm": "workload image 777lefty/wlgen-proc:latest is not in image_cache.list",
    # ingress-nginx is installed from a live helm repo with an unpinned chart.
    # Its controller and admission-webhook images are not in image_cache.list.
    "expired_tls_hotel_reservation": "deploys ingress-nginx; its images are not in image_cache.list",
    "ingress_misroute": "deploys ingress-nginx; its images are not in image_cache.list",
    # FleetCast deploys ingress-nginx first (same uncached images), then the
    # TiDB operator and cluster from the live pingcap and fleetcast chart
    # repos. The nested FleetCast submodule is also needed for the TiDB CR.
    "operator_overload_replicas": "FleetCast app: needs ingress-nginx images and live helm repos",
    "operator_non_existent_storage": "FleetCast app: needs ingress-nginx images and live helm repos",
    "operator_invalid_affinity_toleration": "FleetCast app: needs ingress-nginx images and live helm repos",
    "operator_security_context_fault": "FleetCast app: needs ingress-nginx images and live helm repos",
    "operator_wrong_update_strategy_fault": "FleetCast app: needs ingress-nginx images and live helm repos",
    "operator_wrong_operator_image": "FleetCast app: needs ingress-nginx images and live helm repos",
    # Train Ticket's chart runs a job that applies ~41 microservice manifests
    # at install time. Those images are a documented gap in image_cache.list.
    "trainticket_f17_nested_sql_select_clause_error": "Train Ticket microservice images are not in image_cache.list",
    "trainticket_f22_sql_column_name_mismatch_error": "Train Ticket microservice images are not in image_cache.list",
}

SREGYM_FULL_RUNNABLE_PROBLEMS = tuple(p for p in ALL_PROBLEM_IDS if p not in NOT_RUNNABLE_ON_BENCHMARK_BOX)

PROBLEM_SETS = {
    "sregym-lite": SREGYM_LITE_PROBLEMS,
    # Every registered problem. Includes the ones that cannot run on the
    # benchmark box; use sregym-full-runnable for a wave.
    "sregym-full": ALL_PROBLEM_IDS,
    # sregym-full minus NOT_RUNNABLE_ON_BENCHMARK_BOX.
    "sregym-full-runnable": SREGYM_FULL_RUNNABLE_PROBLEMS,
}


def resolve_problems(spec: str) -> list[str]:
    """Union a comma-separated mix of named sets and bare problem ids.

    Each comma-separated token is looked up first in PROBLEM_SETS (a named
    set expands to every id it contains), otherwise checked as a bare id
    against ALL_PROBLEM_IDS. Duplicates collapse silently, whether they come
    from two sets, a set and one of its own members named again, or a
    repeated bare id: dedupe is the point.

    An unrecognized token is a hard stop, not a silent narrowing: raises
    ValueError naming every offender, so a typo can never shard to nothing
    and read like a clean short run.

    An empty spec is also a hard stop. Nothing here means "the whole
    registry": nobody knows which subset of ALL_PROBLEM_IDS the paper used
    (NOFireAI/brain-go#2221), so there is no full-registry default to fall
    back to.

    Returns the resolved ids sorted, so the same spec always resolves to the
    same list regardless of token order or which sets overlap.
    """
    tokens = [token.strip() for token in spec.split(",") if token.strip()]
    if not tokens:
        raise ValueError("empty problem selection: name a set or a problem id")

    known_ids = set(ALL_PROBLEM_IDS)
    selected: set[str] = set()
    unknown: list[str] = []
    for token in tokens:
        if token in PROBLEM_SETS:
            selected.update(PROBLEM_SETS[token])
        elif token in known_ids:
            selected.add(token)
        else:
            unknown.append(token)

    if unknown:
        raise ValueError("unknown problem set or id: " + ", ".join(unknown))

    return sorted(selected)
