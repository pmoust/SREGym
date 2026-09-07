"""Namespace reconciliation must not delete the infra stack it doesn't know
about because its own baseline was captured before the stack existed.

deploy_app used to call capture_baseline()/save_baseline_state() before
installing metrics-server, OpenEBS, Prometheus, Jaeger, Loki and the MCP
server, so none of their namespaces were ever in the baseline. reconcile_to_
baseline (called between problems) then treated `openebs` and `observe` as
unexpected drift and deleted them every time, forcing a full stack reinstall
per problem instead of once per slot (sregym#56). deploy_app now captures the
baseline after the stack is up; these tests exercise reconcile_to_baseline in
isolation to confirm that once a namespace is part of the baseline, it
survives reconciliation.
"""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sregym.service.cluster_state import ClusterBaseline, ClusterStateManager


def _namespace(name):
    ns = MagicMock()
    ns.metadata.name = name
    return ns


@pytest.fixture
def manager():
    with patch("sregym.service.cluster_state.client") as mock_client:
        mgr = ClusterStateManager(kubectl=MagicMock())

        # Every reconciliation step besides namespaces returns an empty list;
        # only namespace handling is under test here.
        mgr.rbac_v1.list_cluster_role.return_value = MagicMock(items=[])
        mgr.rbac_v1.list_cluster_role_binding.return_value = MagicMock(items=[])
        mgr.core_v1.list_persistent_volume.return_value = MagicMock(items=[])
        mgr.core_v1.list_node.return_value = MagicMock(items=[])
        mgr.storage_v1.list_storage_class.return_value = MagicMock(items=[])
        mgr.apiextensions_v1.list_custom_resource_definition.return_value = MagicMock(items=[])
        mgr.admission_v1.list_validating_webhook_configuration.return_value = MagicMock(items=[])
        mgr.admission_v1.list_mutating_webhook_configuration.return_value = MagicMock(items=[])
        mgr.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
        yield mgr, mock_client


def test_a_namespace_in_the_baseline_survives_reconciliation(manager):
    """openebs/observe, once captured into the baseline after the infra
    stack is up, must not be treated as unexpected drift."""
    mgr, _ = manager
    mgr.baseline = ClusterBaseline(namespaces={"openebs", "observe", "default"})
    mgr.core_v1.list_namespace.return_value = MagicMock(
        items=[_namespace("openebs"), _namespace("observe"), _namespace("default")]
    )

    changes = mgr.reconcile_to_baseline()

    assert changes["namespaces_deleted"] == []
    mgr.kubectl.delete_namespace.assert_not_called()


def test_a_namespace_absent_from_the_baseline_is_still_deleted(manager):
    """The reconciler's actual job: real problem-created drift must still be
    cleaned up. This is the behaviour that must survive the ordering fix."""
    mgr, _ = manager
    mgr.baseline = ClusterBaseline(namespaces={"default"})
    mgr.core_v1.list_namespace.return_value = MagicMock(items=[_namespace("default"), _namespace("some-problem-app")])

    changes = mgr.reconcile_to_baseline()

    assert changes["namespaces_deleted"] == ["some-problem-app"]
    mgr.kubectl.delete_namespace.assert_called_once_with("some-problem-app")


def test_protected_namespaces_are_never_deleted_even_when_not_in_baseline(manager):
    """kube-system et al. must survive regardless of what the baseline says,
    independent of the ordering fix."""
    mgr, _ = manager
    mgr.baseline = ClusterBaseline(namespaces=set())
    mgr.core_v1.list_namespace.return_value = MagicMock(items=[_namespace("kube-system"), _namespace("chaos-mesh")])

    changes = mgr.reconcile_to_baseline()

    assert changes["namespaces_deleted"] == []
    mgr.kubectl.delete_namespace.assert_not_called()
