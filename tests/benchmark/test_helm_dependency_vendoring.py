"""Helm.install must never fetch a chart's dependencies over the network.

A lite benchmark run deploys fully offline. `helm dependency update` reaches
every dependency's repository URL, so `Helm.install` resolves dependencies
from what is already on disk instead: a chart's own `charts/` directory
(checked by name only, the same as a real `helm install` or `helm template`
would use whatever is there without re-validating its version), or
sregym/resources/vendored/charts/<chart name>/, matched on the exact
version declared, when that directory does not have everything a chart
declares.
"""

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from sregym.service import helm as helm_module
from sregym.service.helm import VENDORED_DEPENDENCY_CHARTS_DIR, Helm

REPO_ROOT = Path(__file__).resolve().parents[2]
ASTRONOMY_SHOP_CHART = REPO_ROOT / "SREGym-applications" / "astronomy-shop" / "charts" / "opentelemetry-demo"
PROMETHEUS_CHART = REPO_ROOT / "sregym" / "observer" / "prometheus" / "prometheus"
SOCIAL_NETWORK_CHART = REPO_ROOT / "SREGym-applications" / "socialNetwork" / "helm-chart" / "socialnetwork"


def _make_tgz(tmp_path: Path, name: str, version: str) -> Path:
    chart_dir = tmp_path / f"{name}-src"
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(yaml.safe_dump({"name": name, "version": version}))
    tgz_path = tmp_path / f"{name}-{version}.tgz"
    with tarfile.open(tgz_path, "w:gz") as tar:
        tar.add(chart_dir, arcname=name)
    return tgz_path


def _write_chart(chart_dir: Path, name: str, dependencies: list[dict]):
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        yaml.safe_dump({"apiVersion": "v2", "name": name, "version": "1.0.0", "dependencies": dependencies})
    )


class TestResolveLocalChart:
    def test_no_dependencies_returns_chart_path_unchanged(self, tmp_path):
        chart_dir = tmp_path / "plain-chart"
        _write_chart(chart_dir, "plain-chart", [])

        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(chart_dir))

        assert resolved_path == str(chart_dir)
        assert tmp_dir is None

    def test_repo_coordinate_chart_path_passed_through_unmodified(self, tmp_path):
        """A Helm repo coordinate such as "chaos-mesh/chaos-mesh" has no
        Chart.yaml at that path: ChaosInjector passes exactly this shape,
        with no remote_chart flag set
        (sregym/generators/noise/impl/stress_injector.py). It must reach
        `helm install` unresolved rather than raising FileNotFoundError
        trying to open a Chart.yaml that was never there."""
        resolved_path, tmp_dir = helm_module._resolve_local_chart("chaos-mesh/chaos-mesh")

        assert resolved_path == "chaos-mesh/chaos-mesh"
        assert tmp_dir is None

    def test_dependency_already_present_as_tgz_needs_no_copy(self, tmp_path):
        chart_dir = tmp_path / "chart-with-tgz-dep"
        _write_chart(chart_dir, "chart-with-tgz-dep", [{"name": "dep", "version": "1.2.3"}])
        charts_dir = chart_dir / "charts"
        charts_dir.mkdir()
        _make_tgz(tmp_path, "dep", "1.2.3")
        shutil.copy2(tmp_path / "dep-1.2.3.tgz", charts_dir / "dep-1.2.3.tgz")

        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(chart_dir))

        assert resolved_path == str(chart_dir)
        assert tmp_dir is None

    def test_dependency_already_present_as_unpacked_dir_needs_no_copy(self, tmp_path):
        chart_dir = tmp_path / "chart-with-dir-dep"
        _write_chart(chart_dir, "chart-with-dir-dep", [{"name": "dep", "version": "1.2.3"}])
        dep_dir = chart_dir / "charts" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "Chart.yaml").write_text(yaml.safe_dump({"name": "dep", "version": "1.2.3"}))

        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(chart_dir))

        assert resolved_path == str(chart_dir)
        assert tmp_dir is None

    def test_wildcard_version_matches_unpacked_dir(self, tmp_path):
        chart_dir = tmp_path / "chart-with-wildcard-dep"
        _write_chart(chart_dir, "chart-with-wildcard-dep", [{"name": "dep", "version": "5.15.*"}])
        dep_dir = chart_dir / "charts" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "Chart.yaml").write_text(yaml.safe_dump({"name": "dep", "version": "5.15.3"}))

        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(chart_dir))

        assert resolved_path == str(chart_dir)
        assert tmp_dir is None

    def test_a_committed_subchart_ahead_of_its_own_chart_lock_still_counts_as_present(self, tmp_path):
        """`helm install`/`helm template` never re-validates an
        already-packaged or already-unpacked subchart against the parent's
        declared version; presence is checked by name only. Without this, a
        chart committed with a dependency one version ahead of its own
        Chart.lock (drift, not a missing dependency: sregym/observer/
        prometheus/prometheus's own prometheus-node-exporter subchart is
        4.24.0 on disk against a 4.23.* constraint) would wrongly be
        reported as needing to be vendored."""
        chart_dir = tmp_path / "chart-with-drifted-dep"
        _write_chart(chart_dir, "chart-with-drifted-dep", [{"name": "dep", "version": "4.23.*"}])
        dep_dir = chart_dir / "charts" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "Chart.yaml").write_text(yaml.safe_dump({"name": "dep", "version": "4.24.0"}))

        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(chart_dir))

        assert resolved_path == str(chart_dir)
        assert tmp_dir is None

    def test_missing_dependency_vendored_elsewhere_is_copied_into_scratch_chart(self, tmp_path, monkeypatch):
        vendored_root = tmp_path / "vendored"
        chart_name = "chart-needing-vendoring"
        vendored_dir = vendored_root / chart_name
        vendored_dir.mkdir(parents=True)
        vendored_tgz = _make_tgz(tmp_path, "dep", "9.9.9")
        shutil.copy2(vendored_tgz, vendored_dir / "dep-9.9.9.tgz")
        monkeypatch.setattr(helm_module, "VENDORED_DEPENDENCY_CHARTS_DIR", vendored_root)

        chart_dir = tmp_path / chart_name
        _write_chart(chart_dir, chart_name, [{"name": "dep", "version": "9.9.9"}])

        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(chart_dir))

        try:
            assert resolved_path != str(chart_dir)
            assert (Path(resolved_path) / "charts" / "dep-9.9.9.tgz").exists()
            # The original checkout is untouched.
            assert not (chart_dir / "charts").exists()
        finally:
            if tmp_dir is not None:
                tmp_dir.cleanup()

    def test_missing_dependency_not_vendored_fails_loud_and_names_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(helm_module, "VENDORED_DEPENDENCY_CHARTS_DIR", tmp_path / "empty-vendored-root")

        chart_dir = tmp_path / "chart-missing-dep"
        _write_chart(chart_dir, "chart-missing-dep", [{"name": "nowhere-to-be-found", "version": "1.0.0"}])

        with pytest.raises(RuntimeError, match="nowhere-to-be-found 1.0.0"):
            helm_module._resolve_local_chart(str(chart_dir))

    def test_install_never_shells_out_to_dependency_update(self, tmp_path, monkeypatch):
        chart_dir = tmp_path / "no-op-chart"
        _write_chart(chart_dir, "no-op-chart", [])

        commands = []

        class _FakeProcess:
            returncode = 0

            def communicate(self):
                return b"", b""

        def fake_popen(command, **kwargs):
            commands.append(command)
            return _FakeProcess()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        Helm.install(release_name="r", chart_path=str(chart_dir), namespace="ns", remote_chart=False)

        assert len(commands) == 1
        assert "dependency update" not in commands[0]
        assert "helm install" in commands[0]

    def test_install_repo_coordinate_with_no_remote_chart_flag_does_not_crash(self, monkeypatch):
        """Regression test for the ChaosInjector caller shape: a repo
        coordinate chart_path with no remote_chart flag set must still
        reach `helm install`, not raise FileNotFoundError before any
        subprocess runs."""
        commands = []

        class _FakeProcess:
            returncode = 0

            def communicate(self):
                return b"", b""

        def fake_popen(command, **kwargs):
            commands.append(command)
            return _FakeProcess()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        Helm.install(release_name="chaos-mesh", chart_path="chaos-mesh/chaos-mesh", namespace="ns")

        assert len(commands) == 1
        assert "chaos-mesh/chaos-mesh" in commands[0]


@pytest.mark.skipif(
    not ASTRONOMY_SHOP_CHART.exists(), reason="SREGym-applications/astronomy-shop submodule not checked out"
)
class TestAstronomyShopVendoredDependencies:
    def test_every_declared_dependency_has_a_vendored_tgz_at_the_pinned_version(self):
        with open(ASTRONOMY_SHOP_CHART / "Chart.yaml") as f:
            chart_yaml = yaml.safe_load(f)

        vendored_dir = VENDORED_DEPENDENCY_CHARTS_DIR / "opentelemetry-demo"
        assert vendored_dir.is_dir(), f"missing {vendored_dir}"

        for dependency in chart_yaml["dependencies"]:
            name, version = dependency["name"], str(dependency["version"])
            expected = vendored_dir / f"{name}-{version}.tgz"
            assert expected.exists(), f"missing vendored dependency: {expected}"

    def test_resolve_local_chart_vendors_without_touching_the_submodule_checkout(self):
        resolved_path, tmp_dir = helm_module._resolve_local_chart(str(ASTRONOMY_SHOP_CHART))
        try:
            assert resolved_path != str(ASTRONOMY_SHOP_CHART)
            resolved_charts_dir = Path(resolved_path) / "charts"
            with open(ASTRONOMY_SHOP_CHART / "Chart.yaml") as f:
                dependencies = yaml.safe_load(f)["dependencies"]
            for dependency in dependencies:
                name, version = dependency["name"], str(dependency["version"])
                assert (resolved_charts_dir / f"{name}-{version}.tgz").exists()
            assert not (ASTRONOMY_SHOP_CHART / "charts").exists()
        finally:
            if tmp_dir is not None:
                tmp_dir.cleanup()


@pytest.mark.skipif(not PROMETHEUS_CHART.exists(), reason="prometheus chart not present")
def test_prometheus_chart_already_satisfies_its_own_dependencies():
    resolved_path, tmp_dir = helm_module._resolve_local_chart(str(PROMETHEUS_CHART))
    if tmp_dir is not None:
        tmp_dir.cleanup()
    assert resolved_path == str(PROMETHEUS_CHART)


@pytest.mark.skipif(
    not SOCIAL_NETWORK_CHART.exists(), reason="SREGym-applications/socialNetwork submodule not checked out"
)
def test_social_network_chart_already_satisfies_its_own_dependencies():
    resolved_path, tmp_dir = helm_module._resolve_local_chart(str(SOCIAL_NETWORK_CHART))
    if tmp_dir is not None:
        tmp_dir.cleanup()
    assert resolved_path == str(SOCIAL_NETWORK_CHART)
