"""A persisted baseline file written by pre-#56 code has no schema_version
field at all, and it captured the cluster before the infra stack deployed --
missing exactly the namespaces (openebs, observe, ...) the ordering fix in
sregym#56 now expects the baseline to carry. Loading it as-is would silently
reproduce that bug on every run until the file is manually cleared.
load_baseline_state() must reject a file whose schema_version does not match
current code and fall back to recapturing, the same path a first boot with
no file at all takes.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from sregym.service.cluster_state import BASELINE_STATE_VERSION, ClusterBaseline, ClusterStateManager


@pytest.fixture
def manager():
    with patch("sregym.service.cluster_state.client"):
        yield ClusterStateManager(kubectl=MagicMock())


def test_a_file_with_no_schema_version_is_rejected(manager, tmp_path):
    """Exactly the shape pre-#56 code wrote: every real field, no version key
    at all."""
    path = tmp_path / "baseline.json"
    stale = ClusterBaseline(namespaces={"default"}).to_json()
    del stale["schema_version"]
    path.write_text(json.dumps(stale))

    assert manager.load_baseline_state(path) is False
    assert manager.baseline is None, "a rejected file must not become the active baseline"


def test_a_file_with_an_older_schema_version_is_rejected(manager, tmp_path):
    path = tmp_path / "baseline.json"
    stale = ClusterBaseline(namespaces={"default"}).to_json()
    stale["schema_version"] = BASELINE_STATE_VERSION - 1
    path.write_text(json.dumps(stale))

    assert manager.load_baseline_state(path) is False
    assert manager.baseline is None


def test_a_file_with_the_current_schema_version_loads(manager, tmp_path):
    path = tmp_path / "baseline.json"
    current = ClusterBaseline(namespaces={"openebs", "observe", "default"}).to_json()
    assert current["schema_version"] == BASELINE_STATE_VERSION
    path.write_text(json.dumps(current))

    assert manager.load_baseline_state(path) is True
    assert manager.baseline.namespaces == {"openebs", "observe", "default"}


def test_save_then_load_round_trips_through_the_version_check(manager, tmp_path):
    """The real path a fresh capture takes: save_baseline_state() writes a
    file that load_baseline_state() can then load back, on the next problem
    in the same slot."""
    path = tmp_path / "baseline.json"
    manager.core_v1.list_namespace.return_value = MagicMock(items=[])
    manager.rbac_v1.list_cluster_role.return_value = MagicMock(items=[])
    manager.rbac_v1.list_cluster_role_binding.return_value = MagicMock(items=[])
    manager.core_v1.list_persistent_volume.return_value = MagicMock(items=[])
    manager.storage_v1.list_storage_class.return_value = MagicMock(items=[])
    manager.apiextensions_v1.list_custom_resource_definition.return_value = MagicMock(items=[])
    manager.admission_v1.list_validating_webhook_configuration.return_value = MagicMock(items=[])
    manager.admission_v1.list_mutating_webhook_configuration.return_value = MagicMock(items=[])
    manager.core_v1.list_node.return_value = MagicMock(items=[])
    from kubernetes.client.rest import ApiException

    manager.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)

    manager.save_baseline_state(path)
    manager.baseline = None  # simulate a fresh process picking the file back up

    assert manager.load_baseline_state(path) is True
