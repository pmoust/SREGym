# Vendored manifests and charts

Third-party Kubernetes manifests and Helm dependency charts the benchmark box
used to fetch from a remote URL at deploy time. Vendoring them means a
benchmark run never depends on GitHub, openebs.github.io, a Helm chart
repository, or any other upstream being reachable or unchanged mid-run.
`scripts/benchmark/image_cache.list`'s own tests (`tests/benchmark/
test_image_cache_completeness.py`) scan these files for image literals the
same way they scan everything else under `sregym/` and `kind/`.

## Manifests

| File | Source | Version | Applied by |
| --- | --- | --- | --- |
| `metrics-server-components-v0.9.0.yaml` | `https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml` | v0.9.0 | `sregym/conductor/conductor.py`'s `deploy_app` |
| `openebs-operator-v3.5.0.yaml` | `https://openebs.github.io/charts/openebs-operator.yaml` | 3.5.0 (`openebs.io/version` label; this is the "latest" that URL resolved to when vendored) | `sregym/conductor/conductor.py`'s `deploy_app` |
| `tidb-operator-crd-v1.6.3.yaml` | `https://raw.githubusercontent.com/pingcap/tidb-operator/v1.6.3/manifests/crd.yaml` | v1.6.3 | `sregym/service/apps/tidb_cluster_operator.py`'s `install_crds`, via `sregym/service/metadata/tidb_metadata.json` (the one a real benchmark run loads, through `sregym/observer/tidb_cluster_deploy_helper.py`) |
| `tidb-cluster-recovery-v1.6.0.yaml` | `https://raw.githubusercontent.com/pingcap/tidb-operator/v1.6.0/examples/basic/tidb-cluster.yaml` | v1.6.0 | `sregym/generators/fault/inject_operator.py`'s `recover_fault` and the overload-replicas recovery path (the clean CR a TidbCluster-targeting fault restores) |

`sregym/service/metadata/tidb-with-operator.json`'s `Helm Operator Config.CRD`
field names `tidb-operator-crd-v1.6.0.yaml`, which is not vendored here:
nothing in the repo constructs `TiDBClusterDeployer` with that metadata file
(`sregym.paths.TIDB_METADATA` points at it but is itself unused), so there is
no live path to keep it consistent against. Vendor that file, from the same
URL pattern as the v1.6.3 row above with `v1.6.0` substituted in, before
wiring `tidb-with-operator.json` up to anything real.

Calico's manifest is vendored separately, at `kind/manifests/calico-v3.27.0.yaml`,
because `kind/setup_kind_cluster.sh` is the only thing that applies it.

### Refreshing a manifest

1. Decide the new version and fetch the manifest from the source URL above
   with that version substituted in.
2. Diff it against the file here. Check `image:` keys and any env `value:`
   that looks like an image reference (the OpenEBS operator sets its helper
   image through `OPENEBS_IO_HELPER_IMAGE`/`CLEANUP_JOB_IMAGE` env vars, not
   an `image:` key) against `scripts/benchmark/image_cache.list`; add or
   update any entry the new version needs.
3. Replace the file, rename it to match the new version, and update every
   caller's path and this table.
4. Re-run `tests/benchmark/test_image_cache_completeness.py`: it fails if the
   new file references an image not in the list.
5. Re-run `scripts/benchmark/seed_image_cache.sh` so the new images are
   actually in the S3 cache before a box reads them.

## Dependency charts (`charts/`)

`sregym/service/helm.py`'s `Helm.install` never runs `helm dependency
update`: a lite benchmark run must not fetch a chart's dependencies from a
Helm repository mid-run. For a local chart (`remote_chart: false`), it reads
the chart's own `Chart.yaml` and checks every declared dependency is already
present in that chart's `charts/` directory; a dependency that is not gets
copied in from `charts/<chart name>/` here instead, and installed from a
scratch copy so the checked-out chart on disk is left untouched. A
dependency present in neither place fails the install before any `helm`
subprocess runs, naming the chart and the dependency.

Covers sregym-lite only: the charts below are the ones lite's current 21
problems load. A problem outside lite that also declares remote Helm
dependencies is tracked at NOFireAI/sregym#152, not handled by this
directory; so is a periodic check that these pinned versions still match
what upstream resolves to today (NOFireAI/sregym#153).

| Chart directory | For | Pinned at | Source |
| --- | --- | --- | --- |
| `opentelemetry-demo/` | `SREGym-applications/astronomy-shop/charts/opentelemetry-demo` (submodule commit `7d7b0747`, 7 of 21 lite problems) | the versions in that chart's own `Chart.lock`: `opentelemetry-collector` 0.142.2, `jaeger` 4.3.0, `prometheus` 28.2.0, `grafana` 10.5.8, `opensearch` 3.4.0 | `helm pull <name> --repo <url> --version <version>`, repository URLs from the chart's `Chart.yaml` |

`sregym/observer/prometheus/prometheus` and
`SREGym-applications/socialNetwork/helm-chart/socialnetwork` need no entry
here: both already carry every declared dependency as a committed chart
(an unpacked `charts/<name>/` directory for the first, a packaged
`charts/<name>-<version>.tgz` for the second), so `Helm.install` finds them
without needing anything from this directory.

### Refreshing a dependency chart

1. Read the target chart's `Chart.yaml` (and `Chart.lock`, if present) for
   the exact dependency names, versions and repository URLs.
2. `helm pull <name> --repo <repository> --version <version>` for each one
   that is not already satisfied by the chart's own `charts/` directory.
3. Replace the file(s) under `charts/<chart name>/` here and update the
   table above.
4. Verify offline: `HELM_REPOSITORY_CACHE=<empty dir>
   HELM_REPOSITORY_CONFIG=<empty dir>/repositories.yaml helm template
   <release> <path to a scratch copy of the chart, with these tgz files
   copied into its charts/ directory>` must succeed with no network access,
   and every `image:` it renders must already be in
   `scripts/benchmark/image_cache.list`.
5. Re-run `tests/benchmark/test_image_cache_completeness.py` and the
   `Helm.install` dependency tests in `tests/service/`.
