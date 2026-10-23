# NOFire private mirror of SREGym

This is a **private mirror** of [SREGym/SREGym](https://github.com/SREGym/SREGym)
(MIT-licensed — see `LICENSE`), plus NOFire's brain agent driver and benchmark
harness. It is a detached mirror, not a GitHub fork: forks of public repos
cannot be private, and the driver/harness must stay private.
Context: [NOFireAI/engineering#800](https://github.com/NOFireAI/engineering/issues/800).

## Branch layout

- `upstream-main` — pristine copy of upstream `main`. Never commit here;
  force-updated on every sync.
- `main` — `upstream-main` + NOFire commits. Protected against force-push and
  deletion; merge-only history.

## NOFire-specific changes

- `clients/brain/driver.py` — brain agent driver + testbed harness
  (`stabilize_flaky_pods`, `stellarize_testbed`, `check_testbed_validity`,
  `wait_for_fault_synced`, and the `SREGYM_*` knobs).
- `llm_backend/claude_cli_backend.py` — judge backend that shells out to the
  Claude Code CLI (`claude_cli/<model>` judge model id or
  `JUDGE_PROVIDER=claude_cli`).
- `sregym/conductor/conductor.py` — env-gated baseline-sync wait before fault
  injection (`SREGYM_BASELINE_SYNC_SECONDS`), the native arm's gate at the
  end of that wait, and the native arm's post-fault wait right after
  injection. The baseline wait is a candidate for upstreaming; the gate and
  the post-fault wait are not, since they read brain-go. `_cleanup_sync()`
  checks observed pod state for an image-pull failure via
  `_detect_image_pull_harness_failure()` before `recover_fault()` runs, then
  folds a problem's own `harness_failure` (set by `recover_fault` raising)
  into the attempt's results after `recover_fault()` returns. See the
  harness_failure entry below (NOFireAI/sregym#145, #150). All of this runs
  for every arm and is an upstreaming candidate: none of it reads brain-go.
- **`harness_failure` (NOFireAI/sregym#145, #150).** Two harness failures
  were costing whole attempts and reading as agent regressions: `wrk2-job`
  sitting in ImagePullBackOff because `deathstarbench/wrk2-client` had never
  been pulled onto a cold kind node, and `rolling_update_misconfigured`'s
  standalone `custom-service` baseline deployment (`python:3.9-slim`) missing
  its 120s healthy-rollout window for the same reason. The original fix
  (#145) pre-pulled a problem's images onto every kind node before its fault
  ran; #150 removed that per-attempt pre-pull (see the registry mirror entry
  below for why and for the replacement) and reworked only the detection
  side to not depend on a pre-pull list. What is still here, unchanged by
  #150:
  - A `harness_failure` field (`image_pull`, `deploy_failed`,
    `recovery_error`), recorded per attempt so scoring can tell "the harness
    never let this run cleanly" apart from "the agent reasoned badly".
    `deploy_failed` is set in `main.py`'s `_deploy_failed_snapshot()`
    (extracted from the deploy-retry loop for testability) on every row
    whose deploy exhausted all 3 retries, for any of the reasons
    `start_problem()` can fail that many times: a readiness wait timing out,
    a `Helm.install` `RuntimeError`, the native arm's own gate abort, or
    `inject_fault()` itself raising. Every one of those means the attempt
    never ran at all. `recovery_error` is set by
    `sregym/utils/decorators.py`'s `mark_fault_injected`: when a
    `recover_fault` raises (previously logged as a warning and silently
    swallowed with no trace left anywhere), it now also records
    `self.harness_failure = "recovery_error"` on the `Problem` instance,
    which `sregym/conductor/problems/base.py`'s `fold_harness_failure()`
    copies into the conductor's results after cleanup. First harness_failure
    recorded in an attempt wins (the image-pull check runs before
    `recover_fault`, so it wins over a `recovery_error` found afterward),
    since a recovery problem found during cleanup is usually a secondary
    symptom of whatever already went wrong, not a second, unrelated cause.
    `recovery_error` is reported like the others but, unlike `image_pull`,
    does not remove the attempt from `graded`: see the `score_run.py` entry
    below for why.
  - The recovery-error finding this surfaced:
    `rolling_update_misconfigured`'s `recover_rolling_update_misconfigured`
    (`inject_virtual.py`) tried to `kubectl apply` a baseline manifest
    (`/tmp/<service>-orig.yaml`) that `inject_rolling_update_misconfigured`
    only ever writes *after* the baseline rollout succeeds. When the baseline
    timed out, the file was never written, and the apply against a
    nonexistent path failed as a plain `kubectl` stderr string (`exec_command`
    returns that string rather than raising), so recovery looked like it ran
    cleanly while actually doing nothing. It now raises a clear
    `FileNotFoundError` before attempting the apply, so `mark_fault_injected`
    can record it as above. The faulty deployment is still deleted either
    way. `inject_rolling_update_misconfigured` also now unlinks
    `/tmp/<service>-orig.yaml` at the very start of injection, before the
    baseline rollout: the file is never deleted on its own, so a leftover
    from an earlier attempt (a different namespace, a different baseline)
    would otherwise pass the `FileNotFoundError` guard above and get applied
    against the wrong cluster state.
  - `sregym/generators/fault/inject_virtual.py`'s
    `_wait_for_deployment_rollout` (used by `rolling_update_misconfigured`'s
    standalone baseline deployment) logs rollout status and every pod's
    full `kubectl describe pod` output (matched by the `app=<service>`
    label) before raising its `TimeoutError`, which previously named no pod
    and no reason at all. `sregym/service/kubectl.py`'s `wait_for_ready`
    (used by every app's own Helm-chart readiness wait) also logs each
    blocked container's full, untruncated kubelet message at its own
    timeout, alongside the existing 160-char-truncated one-line summary
    `blocking_reason()` already put in the raised exception.
  - `scripts/benchmark/score_run.py` reports every `harness_failure` kind as
    its own category (count, breakdown by kind, per-attempt list), always
    counted toward `strict_pct` the same way an absent verdict already is.
    Only `image_pull` (`GRADED_EXCLUDING_HARNESS_FAILURES`, a named constant)
    is also excluded from `graded_pct`/`graded`. `recovery_error` is not:
    `recover_fault` runs during cleanup, after both Diagnosis and Mitigation
    are already graded, so it cannot have affected either verdict, and
    excluding it would bias the remaining pool, since a recovery is more
    likely to fail after the agent's own mitigation changed the deployment
    (a mitigated attempt), not before. `run_stats.py` needed its own small
    fix to match: `group_report()`'s `graded_ci95` now pairs `wilson_ci` with
    `graded_correct`, not `correct`, so the Wilson interval's numerator
    matches a denominator an `image_pull` row may have shrunk.
    `readout_run.py` computes no pass rate and needed no change. All of this
    is backward compatible with a CSV that predates the `harness_failure`
    column: `_val()`'s `.get()` already reads a missing key the same as an
    empty one.

  What #150 changed: `image_pull` used to mean a pod stuck pulling an image
  that was specifically in this attempt's own pre-pull list. With no
  pre-pull list to key off, `sregym/conductor/image_pull_detection.py`
  inverts the rule to an exclude list instead of an allow list. At the start
  of `_cleanup_sync`, before `recover_fault` runs,
  `Conductor._detect_image_pull_harness_failure` lists pods across the app's
  own namespace(s) (this also covers the wrk2 job pod, which runs in the
  app's namespace) and flags the first container or init container waiting
  on `ErrImagePull`/`ImagePullBackOff` whose image is not one of four named,
  fixed exceptions: `inject_app.INCORRECT_IMAGE_DEFAULT`
  (`"app-image:latest"`, a tag that resolves on no registry, the fault for
  the incorrect_image family of problems), `workload_imbalance.
  KUBE_PROXY_REPLACEMENT_IMAGE` (a real but degraded kube-proxy build, the
  fault for `WorkloadImbalance`; injected into `kube-system`, outside any
  app's own namespace, so today's namespace-scoped scan would not reach it
  anyway, excluded in case that scope ever widens),
  `inject_virtual.CUSTOM_SERVICE_HANG_INIT_IMAGE` (`"busybox:1.36"`, the
  rolling_update_misconfigured fault's init container, which sleeps forever
  by design and is not an image-pull problem even though it keeps the pod
  pending), and `inject_operator.WRONG_OPERATOR_IMAGE`
  (`"pingcap/tidb-operatorr:v1.6.3"`, typo in "operatorr", a tag that
  resolves on no registry, the fault for `operator_wrong_operator_image`).
  `sregym/conductor/image_pull_detection.py`'s own module docstring, not
  this list, is the source of truth for the current count; it only grows
  when a new fault needs it, so it is tracked where `FAULT_INTRODUCED_IMAGES`
  is actually defined rather than copied here.

  A stuck image also has to have been part of the app's deployed workload
  before the agent's first stage got control, independent of the fixed
  exceptions above: `Conductor._inject_fault` snapshots every container and
  init-container image across the app's own namespace(s) right after the
  fault runs, before any stage starts, and
  `_detect_image_pull_harness_failure` only flags an image that was in that
  snapshot (plus the image cache, see below). This closed a real gap: an
  image the agent itself introduces
  later, for example a bad tag it patches into a deployment during
  mitigation, used to read as `image_pull`, which both mislabeled a bad
  mitigation as a harness problem and, since `image_pull` is excluded from
  `graded` (see `score_run.py` below), dropped that attempt from scoring
  entirely, inflating the measured pass rate by removing a genuinely bad
  outcome instead of counting it.

  `Conductor._snapshot_namespace_images` (the method behind that snapshot)
  reads two sources per namespace, not one: every live pod's own spec, and
  the pod template of every Deployment, StatefulSet, DaemonSet, ReplicaSet,
  Job and CronJob (a CronJob's template lives one level deeper, at
  `spec.job_template.spec.template.spec`). Live pods alone missed anything
  whose pod does not exist yet at snapshot time (a CronJob that has not
  fired, or a controller whose pods were still being created): a stuck pull
  on one of these read as agent-introduced for the same reason a bad
  mitigation tag used to,
  silently dropping a real harness failure from scoring instead of flagging
  it. `_detect_image_pull_harness_failure` also unions in every image
  `scripts/benchmark/image_cache.list` names (`Conductor._harness_cache_images`,
  in both the fully-qualified form that file spells a `docker.io` path out in
  and the short form this codebase's own source actually writes), since an
  oracle or probe pod the harness deploys outside the app's own namespace(s)
  is still harness-owned and the namespace-scoped snapshot can never see it,
  no matter which object kinds it covers. When any part of the snapshot
  fails (a listing call raises, or the problem has no `.app` to resolve
  namespaces from), `self.results["pre_agent_snapshot"] = "failed"` is set,
  and `_detect_image_pull_harness_failure` (`conductor.py`'s
  `_harness_cache_images()` call) reads that back to fall back to the static
  harness cache list alone, dropping the pre-agent snapshot's own images
  from the union rather than widening to `known_images=None`. `None` used to
  mean "every non-excluded image is eligible to be flagged", which does the
  opposite of what a snapshot failure should do: it flags an agent's own bad
  mitigation tag (a bad image patched into a deployment during mitigation)
  as `harness_failure` too, which excuses it from scoring
  (`score_run.py`'s `GRADED_EXCLUDING_HARNESS_FAILURES` drops it from
  `graded`) instead of counting it as the agent's mistake. The static
  harness cache is narrower than a real pre-agent snapshot (it cannot see an
  oracle or probe pod's image that happens not to be statically listed), so
  a snapshot failure can still occasionally miss a genuine harness failure,
  but no longer manufactures a false one out of the agent's own choice.
  `self.results["pre_agent_snapshot"]` is itself surfaced in the run results
  (`main.py` writes `self.results` into the per-attempt CSV), so a run where
  this happened is visible rather than silently indistinguishable from a
  clean one.

  Every other image a container is observed waiting on, that was already
  part of the deployed workload before the agent started, is harness-owned
  by construction: it is something the deploy itself needed, not a fault the
  problem planted and not something the agent introduced. This also fixes a
  gap the pre-pull-era detector had and documented rather than fixed: it
  only read `container_statuses`, so a stuck init container was a false
  negative; the exclude-list detector reads `init_container_statuses` too.
  The one init container expected to hang (rolling_update_misconfigured's
  `busybox:1.36`, running `sleep infinity`) needs no exclusion: once its
  image is present it is running, never in an image-pull waiting reason.
  Excluding it would also hide a real cache miss on `busybox:1.36`, the
  probe image many oracles use.
  `sregym/conductor/problems/base.py`'s `Problem.EXTRA_PREPULL_IMAGES` and
  `sregym/service/kubectl.py`'s `list_docker_nodes()` (the pre-pull step's
  only other caller) are both gone with the module that used them.
- **Registry mirror and the S3 image cache (NOFireAI/sregym#150).** The
  per-attempt pre-pull above (#145) warmed every kind node's cache before
  every attempt, on every arm, and in the problem-validation workflow. This
  was a fix for one symptom (a cold pull losing a race against a fixed timeout)
  that created a worse one: putting every image on all 4 of a cluster's
  nodes, 4x the disk a cluster actually needs, filled a GitHub Actions
  runner's disk and killed a problem-validation run with "No space left on
  device". It was also solving the wrong layer. A kind cluster is created
  once per slot per run (`scripts/benchmark/run_shard.sh`, `kind/
  setup_kind_cluster.sh`), so images already stay cached on its nodes across
  every attempt after the first; the per-attempt pre-pull only ever mattered
  for the first attempt of each cluster, and every benchmark run starts on
  fresh EC2 boxes, so the real cost was 12 clusters per wave (4 boxes, 3
  slots each) each separately re-pulling the whole image set from Docker
  Hub, which is the suspected cause of the original `wrk2-client`
  ImagePullBackOff (#145) in the first place.

  The replacement moves the pull off the critical path entirely: every image
  the benchmark needs is seeded into one persistent, account-wide S3 bucket
  once, ahead of time, and every benchmark box serves its kind nodes from
  that bucket read-only, never from Docker Hub, ghcr.io, quay.io, or
  registry.k8s.io directly.

  First-run order, per account: apply the NOFireAI/terraform change that
  creates the cache bucket and grants the benchmark instance role read-only
  access to it (`aws/sandbox/sregym`), run `seed_image_cache.sh` and
  `seed_host_images.sh` once each, then launch a run as usual. The default
  way to run them is `seed_cache_on_ec2.sh` (#158): it scales NOFireAI/
  terraform's `sregym-cache-seeder` Auto Scaling group to 1, runs both on
  that instance through SSM with the instance's own role, which can write
  the bucket, and the instance leaves the group when done, success or
  failure, under a 5-hour cap. In-region, the first full seed (109 images,
  read back, plus host images for two brain-go refs) took 16 minutes; from a
  laptop each attempt took over an hour and two failed (Docker Desktop DNS,
  an SSO session expiring mid-run). Running them from a developer machine
  with AWS credentials for the bucket still works (the first needs `crane`;
  the second needs `docker`). On the seeder the registry containers run on
  docker's bridge network and got no credentials from IMDS there
  (`NoCredentialProviders`, IMDSv2 with hop limit 2; cause not established),
  so `seed_cache_remote.sh` exports the instance role's temporary keys as
  `AWS_*`, which `registry_mirror.sh` passes into the container. `scripts/benchmark/image_cache.list`
  is the single source of what the first script seeds; `terraform/README.md`
  has the same order with the exact commands.

  - The bucket (`sregym-bench-registry-cache-<account-id>`, same
    account-derived naming as the results bucket and for the same reason: a
    run-derived name is a rename on every plan, and a rename is a replace)
    is managed in NOFireAI/terraform's `aws/sandbox/sregym`, not in this
    repo's own `terraform/`. It used to be declared in `terraform/s3.tf`
    directly; moved out because this repo's Terraform applies a fresh,
    empty state on every single run (unlike the account-wide NOFireAI/
    terraform state), so a fixed-name bucket declared there is recreated, or
    fails to, on a second run whose empty state tries to create a bucket the
    first run's state already owns. `terraform/variables.tf`'s
    `registry_cache_bucket` (default `sregym-bench-registry-cache-
    819709950457`) names it for a box; `terraform/iam.tf` grants the path
    where this config creates its own role (`var.instance_profile_name ==
    ""`, the default) read-only access, matching what the bring-your-own-role
    path's external role grants. No
    versioning or lifecycle expiry, unlike results: a registry blob is
    content-addressed and immutable, and a moved tag is handled by
    re-seeding, not by S3 object versions. A benchmark box only ever reads
    it (see "read-only" below); seeding is done by the EC2 seeder's role
    or a developer's own credentials, never through the box's own role.
  - `scripts/benchmark/registry_mirror.sh` starts, stops, and addresses one
    `registry:2` container per upstream host (`docker.io`, `ghcr.io`,
    `quay.io`, `registry.k8s.io`, listed by the `hosts` subcommand), each its own fixed
    local port (5000-5003) and its own S3 key prefix, so no two upstreams'
    repository paths have to be proven never to collide in one shared
    prefix. `start-plain` configures `registry:2` with no `proxy.remoteurl`
    at all, so a cache miss 404s instead of reaching out to the real
    registry, for serving a benchmark box and for `seed_image_cache.sh`'s
    verification pass; `start-store` configures it writable, with no
    `proxy.remoteurl` either, as `crane copy`'s push target when seeding.
    There used to be a third, `start-proxy`, configuring `registry:2` as a
    pull-through (`proxy.remoteurl`) cache over the bucket. Removed:
    distribution 2.8.3, the version this
    pulls, has no `proxy.ttl` config key at all, so a line that believed it
    disabled the default 7-day proxy-cache eviction did nothing, silently,
    and a seed left running or resumed past that window could have expired
    and deleted content a benchmark run was reading mid-run. See the seed
    script entry below for the replacement. Both remaining modes write a
    full generated `config.yml` rather than relying on `registry:2`'s
    env-var flattening for nested keys, which panics ("readonly config key
    must contain additional keys") on
    `REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=true` rather than doing
    what it looks like it should.

    `start-plain` takes an optional network argument, `bridge` (default) or
    `host`. `run_shard.sh` always passes `host`; every other caller
    (`seed_image_cache.sh`, a local smoke test) leaves it at `bridge`. This
    matters because the box's IMDS hop limit
    is 1 (`terraform/compute.tf`), so a registry container published the
    ordinary way, on the docker bridge with `-p`, is one network hop further
    from the metadata endpoint than a process on the host and never gets the
    instance role's credentials. It still looked healthy, because the
    readiness check only hit the registry's base route, which never touches
    S3; the first pull that actually needed the storage backend then failed,
    every time. `host` network mode puts the registry on the box's own
    network namespace instead, the same one IMDS-reaching code on the host
    itself runs in, and the readiness check now polls
    `/v2/_catalog` (which walks the configured storage backend to list
    repositories) rather than `/v2/`, so a credentials or connectivity
    problem fails loudly at startup, with the registry's own log printed by
    the caller, instead of silently inside a slot. Confirmed on a Mac
    (`--network host` does not behave like a real Linux host under Docker
    Desktop, so this part could not be proven end to end there): starting a
    registry with `--network host` listening on a chosen port is unreachable
    from the Mac side at all, which is itself the expected Docker-Desktop
    caveat, not a bug in the script.
  - `scripts/benchmark/seed_host_images.sh` caches every image a box's own
    docker daemon pulls or builds directly, never through the in-cluster
    containerd the mirror above covers: `registry:2` itself (the mirror
    containers have to exist before anything can pull through them, so this
    one pull cannot be moved behind itself), the kind node image
    (`jacksonarthurclark/aiopslab-kind-x86`, box and kind node are always
    amd64; the `-arm` variant is for an Apple Silicon developer's own local
    cluster, a path a benchmark box never takes), and brain-go's own host
    images: every compose
    service with no `build` key (`postgres` -> `pgvector/pgvector`,
    `localstack` -> `localstack/localstack`, `temporal` ->
    `temporalio/auto-setup`, `temporal-ui` -> `temporalio/ui`, `dashboard`
    -> `node`, `entry` -> `caddy`) plus the base images any build-keyed
    service's Dockerfile resolves FROM (`golang`, the `GO_VERSION` build
    stage; `alpine`, the `RUNTIME_IMAGE` runtime stage). This replaced a
    fixed list pinned to whatever `origin/main`
    looked like when this script was last edited with a derivation against
    `--brain-go-ref` (`scripts/benchmark/brain_go_images.py`, reading
    `docker compose config` and the Dockerfile's own ARG defaults from a
    temporary, read-only `git worktree` of that ref): a pinned
    `brain_go_ref` (what a benchmark box actually checks out) can set a
    different `GO_VERSION`, and its compose file a different dashboard/entry
    tag, than `origin/main` does at any later moment this script is read,
    which is exactly what the earlier fixed list had already drifted into
    (confirmed against `run.tfvars`/`smoke-pin.tfvars`'s pinned ref:
    `GO_VERSION=1.26.6`, `node:22.9.0-alpine`, where the fixed list had
    `golang:1.26.9` and no entry at all for `node` or `caddy`, so the
    dashboard and entry images were a live, undocumented Docker Hub pull
    this whole time). The first
    two (`registry:2`, the kind node image) were a documented, deliberate
    gap, pulled from Docker Hub once per box; the owner decided there is no
    exception to "a benchmark run never touches Docker Hub", which the
    brain-go compose stack (every arm runs it) was still violating until
    that round, and, for `dashboard`/`entry`, until this one. Run once per
    `--brain-go-ref`, pulls each image by tag, `docker save`s it, and
    uploads the tarball to `s3://<bucket>/host-images/<name>-<image-id>.tar`
    alongside a `host-images/<name>.digest` pointer holding that same local
    image ID. That image ID is `docker inspect`'s `.Id` field, not a
    registry digest (a registry digest is how a *pull* names an image and is
    not reconstructable from a later `docker load`, while the image ID is),
    but it is not a single fixed thing either: on the classic (graph-driver)
    image store `.Id` is a hash of the image's config JSON, while on the
    newer containerd image store (the default on recent Docker Desktop and
    Docker Engine releases) `.Id` is the manifest/index digest instead. Both
    are locally reproducible and both round-trip through `docker save`/
    `docker load` unchanged, which is all `load_host_image`'s own check
    needs, but neither is a full content-addressed lock of every layer byte;
    a sha256 lock over the whole image is a known gap, not yet built (see
    the `seed_image_cache.sh`/`crane` path above for the harder guarantee
    that *is* built, which this host-image cache does not have).
    `scripts/benchmark/host_image_lib.sh`'s `load_host_image`, shared by
    `run_shard.sh` (the first two images) and `setup_brain_go.sh` (the rest,
    loaded before `make up`), reads the pointer, downloads that exact
    tarball, `docker load`s it, and checks the loaded image's own ID against
    the pointer before either caller does anything with it, failing loudly
    if the pointer, the tarball, or the loaded ID do not match. Every
    benchmark box boots from a fresh EC2 instance, so that tag exists
    locally only because of this load, and `registry_mirror.sh`'s `docker
    run registry:2`, kind's own node creation, and brain-go's `docker
    compose build`/`up` never have a reason to contact a registry for one
    again. Done once per box, not once per slot: later `kind create cluster`
    calls in the same shard reuse the same loaded image.
  - `scripts/benchmark/image_cache.list` is every image, one per line as
    `<host>/<path>:<tag>`. Built from `helm template` of every app's own
    Helm chart (the same derivation the per-attempt pre-pull used to do at
    deploy time, now run once here instead of once per attempt), the raw
    manifests for the two apps that skip Helm (`hotel_reservation`,
    `blueprint_hotel_reservation`), the always-on telemetry stack
    (prometheus, loki, promtail charts; jaeger, otel-collector, khaos raw
    manifests), the wrk2 load generator's own job template, the
    custom-service fault's two hand-built images, the edge agent's pinned
    release, the TidbCluster CR's operator-rendered pd/tikv/tidb images and
    helper default (`SREGym-applications/FleetCast/tidb-operator/
    tidb-cluster.yaml`, confirmed against tidb-operator v1.6.0's own
    defaulting source, not a claim this list's own scanner can verify: that
    submodule is outside `tests/benchmark/test_image_cache_completeness.py`'s
    `sregym/`/`kind/` scan roots), and a grep of every fault injector and
    problem file for a real (not `app-image:latest`-style fake) image
    literal, including the images a fault swaps in on purpose
    (`workload_imbalance`'s kube-proxy replacement, `faulty_image_correlated`'s
    and `update_incompatible_correlated`'s bad-but-real versions), which
    still have to pull successfully for the fault to inject at all. Not
    generated by a script, so a missed image is plausible; the seed script's
    own verification pass is what catches one before it reaches a benchmark
    run, not a claim that this list is exhaustive by construction. `tests/
    benchmark/test_image_cache_completeness.py`'s scan is a second,
    independent check, verifying the lite problem set fully; it scans `.py`
    files for an `--image=<ref>` shell flag inside an f-string and for
    embedded YAML `image:` text (not just real Python dict/kwarg/constant
    syntax), and allows a leading underscore on an `*_IMAGE` constant name.
    `registry:2`
    and the kind node image are cached too (see `seed_host_images.sh`
    above), under a different S3 prefix, not as lines in this list: the HOST
    pulls both directly, so neither is a `<host>/<path>:<tag>` line
    `registry_mirror.sh` could route. The file's remaining documented,
    deliberate gaps: train-ticket's own per-microservice images (applied by
    a post-install Job, `ghcr.io/sregym/train-ticket-deploy`, whose own
    image is cached but whose own `kubectl apply` calls are not visible to
    `helm template`), `ghcr.io/sregym/sregym-mcp:latest` (never pulled
    at all: `build_mcp_image.sh` builds this repo's MCP server locally and
    `kind load`s it with `imagePullPolicy: Never`), and every chart or
    manifest still fetched from a remote Helm repo or a raw GitHub URL at
    deploy time rather than vendored: ingress-nginx, chaos-mesh, the pingcap
    Helm repo's own `pingcap/tidb-operator` chart coordinates (its CRDs and
    recovery CR ARE vendored, the chart itself is not), the fleetcast and
    flight-ticket chart repos, and the TidbCluster CR's own `config_url`.
    None of these run under M2 (`noise=false`, no sregym-lite problem
    deploys them), so with the containerd `_default` host mirror fail-closed
    (see above), enabling noise or running a problem that reaches one of
    these apps fails closed (`ImagePullBackOff`, or a `helm repo add`
    network error upstream of any pull) rather than silently reaching the
    internet. They are also outside `test_image_cache_completeness.py`'s own
    coverage, the same as the train-ticket job's own manifests: none of
    these apps' charts live under `sregym/` or `kind/` the way a vendored
    one does, so the scan cannot see them. Covered by sregym-lite only (see
    the "Helm dependency vendoring" section below): extending this caching
    and vendoring to every problem set is NOFireAI/sregym#152, not this
    gap list. `build_mcp_image.sh`'s
    own build still used to reach Docker Hub for one thing: `mcp_server/
    Dockerfile`'s `FROM python:3.12-slim` base, a plain `docker build` on the
    box's own daemon, outside kind and outside the registry mirror's reach.
    Fixed the same way as brain-go's
    build-base images below: when `SREGYM_REGISTRY_MIRROR_ADDR` is set, the
    script pulls `python:3.12-slim` through the already-seeded docker.io
    mirror and `docker tag`s it to the bare name the Dockerfile asks for
    before building, so `docker build` finds it already present. A no-op on
    a developer machine with no mirror running.
  - Loki and Promtail (the always-on telemetry stack) used to install from
    `grafana/loki` and `grafana/promtail` Helm-repo coordinates with no
    pinned `version`, which needed `helm repo add grafana`/`helm repo
    update` at deploy time and could silently resolve to a different chart
    version between runs. `sregym/service/telemetry/loki.py` now installs
    both from vendored chart archives
    (`sregym/resources/vendored/charts/loki-7.3.0.tgz`,
    `.../promtail-6.17.1.tgz`, checksum-verified against the Helm repo's own
    published index at vendor time) with `version` pinned to match, and no
    longer adds or updates the Grafana Helm repo at all. `helm template`
    against both, with this repo's own values files, renders only images
    already in `image_cache.list`.
  - Helm dependency vendoring: `sregym/service/helm.py`'s `Helm.install`
    never runs `helm dependency update` for a local chart (`remote_chart:
    false`), because that reaches every declared dependency's Helm
    repository mid-run. It reads the chart's own `Chart.yaml` first and
    checks each dependency is already present in that chart's `charts/`
    directory (a packaged `<name>-<version>.tgz` or an unpacked `<name>/`
    subdirectory at that version); a dependency that is not gets copied in
    from `sregym/resources/vendored/charts/<chart name>/` instead, into a
    scratch copy of the chart so the checked-out tree on disk is untouched.
    A dependency present in neither place fails the install, naming the
    chart and the dependency, before any `helm` subprocess runs. Covers
    sregym-lite only: `sregym/observer/prometheus/prometheus` and
    `SREGym-applications/socialNetwork/helm-chart/socialnetwork` already
    carry every declared dependency as a committed chart, so they need
    nothing from the vendored directory; astronomy-shop's
    `opentelemetry-demo` chart (`SREGym-applications/astronomy-shop`
    submodule) does not, so its five dependencies
    (`opentelemetry-collector`, `jaeger`, `prometheus`, `grafana`,
    `opensearch`, each pinned to the version in that chart's own
    `Chart.lock`) are vendored under `sregym/resources/vendored/
    charts/opentelemetry-demo/`. See that directory's README for the
    refresh steps. Extending this to every problem set, and a periodic
    check that these pinned versions still match what upstream resolves to,
    are NOFireAI/sregym#152 and #153.
  - `scripts/benchmark/seed_image_cache.sh` is the one-time seed, run from a
    developer machine with AWS credentials for the bucket, never from inside
    a benchmark run. For each upstream with images in the list: start a
    writable, non-proxying registry (`start-store`) over the bucket, `crane
    copy --platform linux/amd64` every one of its images straight from the
    real upstream into it, stop it. Then, as verification: start a *fresh*,
    read-only registry (`start-plain`) over the same bucket and `crane
    validate --remote` every image through that, which fetches the manifest
    and every blob it references, not just the manifest. This replaced a
    `docker pull`-based seed-then-reverify: `docker pull` skips a layer already present in the local docker
    store, so a verification pull could report success while the bucket was
    missing a blob a different image happened to share, and `docker rmi` of
    one tag does not remove layers other images still reference. `crane`
    never touches a local docker store at all, closing that gap, and needs
    no `docker`: `brew install crane`, or see the script's own header for
    why a container image was not used instead. Exits non-zero naming every
    image that failed either pass. Idempotent: safe to re-run after
    `image_cache.list` gains a line, and that is also when to re-run it.
    There is no background job that keeps the bucket in sync with the list.
    Credentials: on a benchmark box, none are passed to the registry
    container at all, and its S3 client falls back to the instance role via
    the EC2 metadata endpoint (only reachable because `run_shard.sh` starts
    that registry in `host` network mode; see above). From a developer
    machine with an SSO profile, that fallback does not reach
    `~/.aws/sso/cache` the way the AWS CLI does, so `registry_mirror.sh`
    resolves `$AWS_PROFILE` to temporary static keys with `aws configure
    export-credentials` (the same thing the CLI already does internally for
    an SSO profile) and passes those through as plain environment variables,
    which every AWS SDK reads. Verified locally end to end with the
    filesystem storage driver: `crane copy` into a `start-store` registry,
    `crane validate --remote` against a fresh `start-plain` registry over
    the same storage passing for a cached image and failing
    (`MANIFEST_UNKNOWN`) for an uncached one.
  - Path layout matters and is not automatic, which is why the verification
    pass above exists. Confirmed with the filesystem-driver smoke this
    change's own PR ran: a client pulling a Docker Hub "official" image
    (`busybox`, `mongo`, ...) through a mirror whose hostname is not
    literally `docker.io` does not get the implicit `library/` prefix the
    real Docker Hub client applies: `docker pull <mirror>/busybox:1.36`
    fails with `not found`, `docker pull <mirror>/library/busybox:1.36`
    succeeds, and a plain registry started over the exact same storage
    serves that same `library/busybox` path back with no proxy configured
    at all. `containerd`'s own mirror redirection applies the same
    normalization before substituting the endpoint, so this is also what a
    kind node's containerd requests, but it is a real, documented gotcha,
    not a safe assumption, which is why `image_cache.list` spells every
    docker.io path out fully (`docker.io/library/busybox:1.36`, not
    `docker.io/busybox:1.36`) rather than leaving the prefix implicit.
  - `kind/setup_kind_cluster.sh` reads `SREGYM_REGISTRY_MIRROR_ADDR` and, when
    it is set, patches every cluster it creates (all three arms, since all
    three call this one script) two ways, on top of whatever `KIND_CONFIG`
    this call already resolved to: the committed file for the MCP and edge
    arms, or native's own `apiServerAddress`-patched copy for the native arm.

    First, a `containerdConfigPatches` block setting
    `[plugins."io.containerd.grpc.v1.cri".registry] config_path =
    "/etc/containerd/certs.d"`, with no `registry.mirrors` entry at all.
    This replaced an earlier version of this patch that set
    `registry.mirrors."<host>".endpoint` instead (the shape upstream's own
    commented-out `kind-config-*.yaml` block uses), which turned out to be
    broken: containerd v2 (the
    `kindest/node:v1.32.1` base image runs containerd v2.0.2) appends the
    real upstream as a last fallback endpoint for a host configured through
    `registry.mirrors` and silently falls through to it on a 404 instead of
    failing the pull, which defeats the point of a benchmark run never
    touching the public internet. Confirmed locally with two kind clusters
    built from `kindest/node:v1.32.1` against the same local registry,
    seeded with one real image and missing another real one: the
    `registry.mirrors`-patched cluster pulled the missing image anyway, by
    reaching real Docker Hub; the `config_path`-patched cluster failed that
    same pull outright (`not found`), with no fallback attempted.

    Second, a `/etc/containerd/certs.d/<host>/hosts.toml` for each of the
    four upstream hosts, containing only `server = "http://<addr>:<port>"`
    and no upstream `[host]` entry (a bare `server` line with no `[host]`
    table is enough on its own, confirmed locally: containerd treats it as
    the sole endpoint for that host, nothing to fall through to), plus a
    fifth, `/etc/containerd/certs.d/_default/hosts.toml`, pointing at
    `http://127.0.0.1:1` (a loopback port nothing listens on). `_default`
    matters because with no per-host directory
    for a given host, `config_path`'s own fallback is containerd's ordinary,
    unmediated upstream resolution, i.e. a real pull, to the real internet.
    The four hosts.toml files only cover the four upstreams this benchmark
    mirrors; anything else (a fault that swaps in an image from a fifth
    host, a typo, an `image_cache.list` addition that lands before its host
    is wired up here) would otherwise pull from the internet with no failure
    at all. Confirmed locally with `crictl pull` (the path kubelet actually
    uses; `ctr images pull` does not consult this config and is not a valid
    way to test it): removing just the `_default` file let a pull from an
    unlisted host (`public.ecr.aws`) succeed for real; restoring it made
    that same pull fail closed with "connection refused".

    All five files are generated and written before `kind create cluster`
    runs, then mounted onto every node's existing `extraMounts` list (the
    same list `kind-config-*.yaml` already uses for `/run/udev`), not copied
    in with `docker cp` after the cluster exists as an earlier version of
    this patch did. A node starts serving pulls the instant its container
    boots, before a post-create copy step would otherwise get a chance to
    run, so an early pod (kube-proxy, a CNI daemon) scheduled in that window
    pulled through whatever `config_path` resolved to at that moment (an
    empty or absent `certs.d`), the same real-internet gap all over again.
    Mounting removes that window instead of narrowing it. The mounted
    directory is a live bind, not a one-time copy, so it is cleaned up on
    the host side only if `kind create cluster` itself fails (a trap);
    deleting it after a successful create would empty the very mount every
    later pull on that node depends on (confirmed locally: killing the
    script right after a successful create left `/etc/containerd/certs.d`
    empty on the running node). Each slot creates exactly one cluster for
    its whole lifetime (`run_shard.sh`'s `run_slot`, not once per attempt),
    so the directory is reclaimed anyway when the box terminates at the end
    of the shard, the same way the generated `MIRRORED_CONFIG` file already
    is. If a future `KIND_CONFIG` ever has a node with no `extraMounts` key
    to extend, the script fails loudly rather than silently mounting nothing
    for that node.

    Unset (any developer's own machine, with no mirror running), every
    cluster is created byte-for-byte as before: no patch, no behavior
    change, same as before #150.
  - `scripts/benchmark/run_shard.sh` loads and verifies the two host images
    (`seed_host_images.sh` above) first, before anything else in this
    section: the registry mirror containers it starts next are themselves
    one of those two images. It then resolves the box's own RFC1918 address
    (`SREGYM_PRIVATE_IP`, now for every arm, not only native and edge: before
    #150 only those two needed it) once, before any kind cluster. It is how
    a kind node, itself a container on its own `kind` docker network,
    reaches back out to the registry mirror containers the same way
    brain-go's own container already reaches the kind API server through
    this address (see `native_arm.sh`'s `kind-config`). It then starts all
    four upstream registries in plain, `host`-network mode, bound to
    `0.0.0.0` so both the
    box's own loopback (the preflight below) and a kind node (through
    `SREGYM_PRIVATE_IP`) can reach them, and exports
    `SREGYM_REGISTRY_MIRROR_ADDR` for `setup_kind_cluster.sh` above. Before
    any slot starts, it then preflights every image in `image_cache.list`
    against these same registries with `scripts/benchmark/
    registry_preflight.py`, not a plain `docker pull`: a GET of the
    manifest (resolving a manifest list to its linux/amd64 child first) and
    a HEAD of the config blob and every layer blob it names, the same two
    requests a real pull makes short of writing the layer bytes to disk,
    with none of the cost of filling the box's own docker image store for
    images that store never otherwise touches (see that script's own
    header). A plain registry has no
    fallback to the real upstream, so a gap here would otherwise surface as
    a cold ImagePullBackOff deep inside a slot, exactly the failure mode
    #150 exists to remove, not reintroduce by a different path. A missing
    image aborts the whole shard before it starts, naming what is missing,
    rather than failing one attempt well into a run. An `image_cache.list`
    line naming a host the mirror scripts do not recognize also aborts the
    shard rather than being silently skipped: a typo'd host used to drop
    that one image from the preflight check entirely. On a failure, the
    preflight also prints the first failed check's own error output and the
    last 50 lines of that host's registry container log, since a plain
    "missing from the cache" message cannot by itself tell a genuinely
    absent image apart from an S3 permission or
    connectivity error on the registry's own backend.
  - Read-only is enforced four ways, not one: a node's containerd has no
    `registry.mirrors` fallback to reach for in the first place (`config_path`
    and `registry.mirrors` are mutually exclusive, and only `config_path` is
    set, per the `setup_kind_cluster.sh` entry above); `_default`'s loopback
    endpoint fails closed any host not in that config at all; the plain
    registries started on a benchmark box carry no `proxy.remoteurl`, so even
    a request that somehow got past the hosts.toml layer would have nothing
    to fall through to; and the preflight above means nothing should ever
    actually miss in the first place. Between them, every CONTAINER IMAGE a
    kind node pulls resolves against this box's own cache, seeded from the
    one shared bucket, and the box itself never pulls `registry:2` or the
    kind node image from Docker Hub either (`seed_host_images.sh` above).
    Five remote YAML manifests used to be fetched the same way, round
    trip, on every run: `kind/setup_kind_cluster.sh`'s Calico manifest
    (once per cluster), `Conductor.deploy_app`'s metrics-server manifest
    and OpenEBS operator manifest (once per attempt), and the TiDB-operator
    problem's CRDs and fault-recovery CR (`raw.githubusercontent.com`,
    two files). All five are now vendored under `sregym/resources/
    vendored/` and `kind/manifests/` (see that directory's README for
    source, version, and refresh steps) and every caller points at the
    local file instead, so a benchmark run no longer depends on GitHub or
    `openebs.github.io` being reachable mid-run. Because those files live
    under `sregym/` and `kind/`, `test_image_cache_completeness.py`'s scan
    covers their `image:` keys and (for the OpenEBS manifest's
    `OPENEBS_IO_HELPER_IMAGE`/`CLEANUP_JOB_IMAGE` env vars) its new env
    `value:` pattern the same as any other raw manifest in this tree.
  - The edge agent's image (`nofireai/edge:v3.6.0`, `native_arm.sh`) still
    cannot be `kind load`ed (a known, unrelated limitation: kind's import
    path requires `--all-platforms` and the image is a multi-arch manifest
    list, so the node has to pull it itself either way) but is mirrored like
    every other docker.io image since #150: the pull still happens, through
    the box's own cache instead of Docker Hub directly. The effective image
    (`SREGYM_EDGE_IMAGE`, when set, overrides the default) is also checked
    by the registry-cache preflight, so an overridden image that is not
    cached fails the shard before any slot starts rather than mid-run.
  - Every arm (not only native and edge) runs brain-go beside the harness,
    and `setup_brain_go.sh` brings it up with `make up` before `run_shard.sh`
    exists: `make up` runs `docker compose --env-file deploy/.env up -d`
    with no `--profile` flag, against the root `docker-compose.yml`
    (`include:`s `deploy/docker-compose.yml` and
    `deploy/docker-compose.clickstack.yml`) and no override file. Every
    service with no `build` key pulls its own `image:` tag directly
    (`postgres` -> `pgvector/pgvector`, `localstack` -> `localstack/
    localstack`, `temporal` -> `temporalio/auto-setup`, `temporal-ui` ->
    `temporalio/ui`, `dashboard` -> `node`, `entry` -> `caddy`); every
    service with a `build` key (`api`, `chat`, `edge`, `mcp`, `migrate`,
    `seed`, `tworker`) builds from `docker/Dockerfile`, `FROM
    golang:<GO_VERSION>` (build stage) and `FROM alpine:<RUNTIME_IMAGE's
    tag>` (runtime stage). `docker compose build`/`up -d`, on the box's own
    daemon, bypasses kind and the registry mirror entirely, and was the one
    host-level Docker Hub reach every arm still had (`dashboard` and
    `entry` were a second, undocumented instance of the same gap, see
    `seed_host_images.sh` above). `setup_brain_go.sh` derives the exact set with
    `scripts/benchmark/brain_go_images.py` against the ref actually checked
    out at `$BRAIN_GO_DIR` and loads every one of them from the S3
    host-image cache (`seed_host_images.sh` above) before calling `make up`,
    the same `load_host_image` step `run_shard.sh` uses for `registry:2` and
    the kind node image; a gate right after re-checks every derived image
    with `docker image inspect` and dies naming every one still absent,
    together, rather than stopping at the first. Compose's own default
    `pull_policy` ("missing": pull only if no matching local image exists)
    then finds every one of them already present and never asks Docker Hub
    for anything; a derivation failure, a load failure, or a gate miss fails
    this script loudly, before `make up` runs, rather than partway through
    it. brain-go's own Makefile and compose file are not ours to add a
    `--pull never`/`pull_policy: never` to (confirmed against both pinned
    tfvars refs and `origin/main`: neither sets one), so this relies on the
    already-loaded exact tag satisfying the "missing" policy rather than
    forcing the policy itself; the fail-loud load step before `make up`
    is what actually guarantees no pull happens on a benchmark box.
  - Remaining network fetches on a benchmark box's own path are package
    installs, not image or manifest fetches, and are intentionally not
    cached by any of the above: `uv sync` (PyPI, this repo's own Python
    deps), `go mod download` and the private `nofire-code-miner` module over
    ssh (brain-go's own build, `docker/Dockerfile`), `apt-get
    install`/`curl`-installed `kubectl` and `uv` inside `mcp_server/
    Dockerfile`'s build, and the dashboard's own `corepack prepare --activate
    && pnpm install --frozen-lockfile` (npm registry, the `dashboard`
    service's command in brain-go's root `docker-compose.yml`, which runs on
    every `make up` since that service carries no `build` key to cache an
    end result for). None of these are container image pulls, so none
    belong in `image_cache.list` or the host-image cache; they are listed
    here so "never pulls a container image from Docker Hub" is not read as
    "makes no network calls at all".
- `agents.yaml` — `brain` agent entry; `container_isolation: false` on
  host-run agents.
- `main.py` — `SREGYM_SKIP_JUDGE_PREFLIGHT`, the per-attempt `arm-native.json`
  written beside `fault-spec.json` under the native arm, and it sets `API_PORT`,
  `MCP_SERVER_PORT` and friends as defaults rather than overrides so several
  runs can share a host. Candidate for upstreaming.
- `sregym/run_artifacts.py` (`stabilize_run_csv_headers`, called once at the
  end of `main.py`'s driver loop) — reconciles every per-attempt result CSV
  under a run to one shared header. Upstream's `finalize_and_publish` writes
  each attempt's header from whatever fieldnames the driver had accumulated
  so far, so a file published early in a run can be missing a column a later
  attempt's oracle introduced: nine header shapes across 63 files in one run,
  13 to 19 columns wide, 11 with no `TTM` column at all
  ([NOFireAI/sregym#39](https://github.com/NOFireAI/sregym/issues/39)). Not
  benchmark-specific — candidate for upstreaming.
- `llm_backend/init_backend.py` — routes the judge to `ClaudeCliBackend` when
  `is_claude_cli_judge()` says so. Keep that branch when merging upstream: it
  conflicts with any upstream change to `get_llm_backend_for_judge`, and losing
  it fails silently by falling back to LiteLLM.
- `sregym/service/apps/astronomy_shop.py` — drops the flagd-ui sidecar and
  prunes two dead collector exporters. **Not env-gated**, so it applies to every
  run. Revert it for any run that claims to be stock.
- `sregym/service/mcp_server.py` — four changes. The host side of the MCP
  port-forward reads `MCP_SERVER_PORT` instead of hardcoding 9954.
  `SREGYM_EXTERNAL_PORT_FORWARD` (off by default) tells `deploy()` that a
  caller owns the forward, so it probes and reports but never starts, kills or
  replaces one; the benchmark bootstrap owns it for the life of a slot, which
  is longer than one problem. `start_port_forward` returns early when the port
  already serves, before the unconditional kill below it, which used to tear
  down a working forward and then, if all three retries failed, leave none at
  all. And it now accepts a forward only once it serves traffic, polled to a
  `PORT_FORWARD_READY_TIMEOUT` deadline, rather than on `poll() is None`: a
  process that binds but never answers was declared healthy and every later
  call paid a full timeout. And `SREGYM_MCP_IMAGE` (empty by default) makes
  `deploy()` apply a generated kustomize overlay that points the pod at that
  image with `imagePullPolicy: Never`, instead of the committed kustomization's
  `ghcr.io/sregym/sregym-mcp:latest` with `imagePullPolicy: Always`. The
  benchmark sets it, so a pod runs this repo's MCP server rather than
  upstream's published build.

  Two of these are plain bug fixes and the strongest candidates for
  upstreaming: the early return in `start_port_forward` and the serves-traffic
  acceptance. The two flags are mirror-specific. Behaviour with both unset is
  unchanged apart from those two fixes, which is what
  `tests/service/test_mcp_server_port_forward.py` pins.
- `mcp_server/Dockerfile` — one added `COPY atif_converter/`. pyproject.toml
  has listed that package since 2026-07-11 and the Dockerfile never copied it,
  so `uv sync --frozen` fails with "package directory 'atif_converter' does not
  exist" and no image can be built from this tree. Candidate for upstreaming.
- `sregym/paths.py` — `SREGYM_CACHE_DIR` overrides the cache location, so runs
  sharing a host do not share a per-cluster baseline snapshot. Candidate for
  upstreaming.
- `kind/setup_kind_cluster.sh` — `KIND_CLUSTER_NAME`, so a second cluster on the
  same host does not collide with the default name `kind`, and `KIND_CONFIG`,
  so the native benchmark arm can pass a copy of the committed config with the
  API server on the host's private address. Both candidates for upstreaming;
  with neither set the behaviour is unchanged. `SREGYM_REGISTRY_MIRROR_ADDR`
  (NOFireAI/sregym#150, see the registry mirror entry above) is mirror-specific
  and not a candidate: with it unset, every cluster is still created
  byte-for-byte as before.
- `.github/workflows/smoke-test.yml`: before creating the kind cluster, the
  Integration Smoke Test reclaims about 24 GB on `/` by removing toolchains the
  job does not use (Android, .NET, GHC, CodeQL) and pruning Docker, printing
  `df -h /` before and after. One run went from 13 GB free to 37 GB in 118s.
  Four kind nodes, each with its own image store and `/var/log/pods` on `/`,
  had filled the runner's disk
  ([NOFireAI/sregym#109](https://github.com/NOFireAI/sregym/issues/109)). On
  failure it prints `df -h` and `docker system df` for the runner and each
  node. The committed kind configs are unchanged. Candidate for upstreaming,
  since upstream runs the same job.

NOFire-only directories, which add no merge drift because upstream has no such
path:

- `clients/brain_go/` — the brain-go agent driver and its SSE parser, with tests
  in `tests/brain_go/`.
- `scripts/benchmark/` — stands up and seeds brain-go on an instance
  (`setup_brain_go.sh`, `seed_connections.py`), computes the per-run
  environment that lets runs share a host (`run_slot_env.sh`), and deploys the
  MCP server once per instance before any slot starts (`deploy_mcp.py`), which
  is the caller `SREGYM_EXTERNAL_PORT_FORWARD` exists for.
  `setup_brain_go.sh` writes `LOG_LEVEL=debug` into the box's `deploy/.env`,
  so brain-go's api service writes the DEBUG edge ingest line
  (`ingested edge resource graph`, one per accepted publish) and
  `dump_brain_go_logs.sh` carries it into the archived `sregym-brain-go.log`.
  At info that log has no edge line at all (sregym#135). This is the benchmark
  box only; brain-go's default and production level is info, a
  `BRAIN_ENV_EXTRA` entry can override it per run, and a brain-go ref older
  than brain-go#3704 ignores the key.
  Under the native arm it also writes `FEATURE_KUBERNETES_TOOLS_ENABLED=true`:
  brain-go ships its `k8s_*` tool family off, and without the flag the seeded
  kubernetes connection validates healthy while every `k8s_*` call fails
  closed (sregym#139). The MCP arm does not get the flag.
  For both arms it raises `FEDERATION_EVIDENCE_INLINE_MAX_BYTES` to 4 MiB: the
  box has no evidence object store, so with the 256 KiB default a large tool
  result fails its call closed (sregym#141). The cap only decides where a
  snapshot is stored.
  `terraform/variables.tf`'s `reasoning_effort` (sregym#149, default empty)
  threads through `compute.tf` and `bootstrap.sh.tftpl` as `REASONING_EFFORT`
  to `setup_brain_go.sh`, which writes `LLM_EFFORT=<value>` into `deploy/.env`
  when it is set and writes nothing when it is not, so an unset run keeps
  brain-go's own default (medium, brain-go#4093). `setup_brain_go.sh` rejects
  anything other than low, medium or high itself, not only at the terraform
  variable's own plan-time validation, since the script is also the
  local-iteration path `scripts/benchmark/README.md`'s "iterate locally
  first" section calls directly, which never goes through terraform.
  `write_run_manifest.sh` records `reasoning_effort` by reading `LLM_EFFORT`
  back from the box's own `deploy/.env` rather than from the terraform
  variable, so a `BRAIN_ENV_EXTRA` override of `LLM_EFFORT` (set_env_key
  deletes-then-appends) is what the manifest reports; empty when the key is
  absent from `deploy/.env`.
  It also holds the native arm (`SREGYM_ARM=native`, brain-go#2410): `arm.py`
  resolves the flag, `seed_native_connections.py` registers brain-go's own
  Kubernetes, Prometheus, Loki and Jaeger connections in place of the four
  CUSTOM_MCP ones, `mint_edge_key.py` mints the edge-type key the in-cluster
  agent publishes with, `native_arm.sh` does the cluster-side work (API server
  address, read-only ServiceAccount, telemetry forwards, the Helm install),
  and `native_gate.py` plus `brain_graph.py` gate and record the entity graph.
  The kubernetes connection's config also sets `configmap_values: "screened"`
  (brain-go#4052), so arm B's `k8s_describe` shows a ConfigMap's actual data
  with secrets masked rather than only key names and byte sizes. A brain-go
  predating #4052 ignores the key: `New()` reads a fixed set of named config
  keys off the connection's config and never rejects one it does not
  recognise. `SREGYM_K8S_CONFIGMAP_VALUES`
  (`seed_native_connections.py`'s `resolve_configmap_values`) overrides the
  default the same way `SREGYM_BASELINE_SYNC_SECONDS` does: read only from
  the environment of the process that runs `seed_slot.sh` on the instance,
  since neither `launch.sh` nor `terraform/templates/bootstrap.sh.tftpl`
  forwards it. `write_run_manifest.sh` records the value a run actually
  seeded as `seeded_configmap_values` (native arm only).
  `install-edge` does not trust `helm --wait`: chart 0.5.2 runs one replica
  with `maxUnavailable: 1`, so Helm 3 counts it ready with zero ready pods.
  After the release it waits for the agent's pod to be Ready with no restarts
  (180s) and for brain-go to hold at least one entity for the slot's account
  (150s, read through `brain_graph.py`). On a timeout or a restart it writes
  the namespace's pods, its last 30 events and the agent's logs to the slot
  log and aborts the slot. The agent publishes every 60s, which is its floor:
  it exits at startup on a shorter interval (sregym#133). The image is
  pinned to `nofireai/edge:v3.6.0` rather than `:latest`, and a values file
  adds the `endpoints` and `networkpolicies` informers that chart 0.5.2's
  resource list drops, plus the `endpoints` RBAC rule the chart does not
  grant (sregym#143).
  After the fault is injected the conductor waits, native and edge arms only
  and capped at 120s, until brain-go holds a change event from after the
  fault, on the fault's component or anywhere in the cluster, so diagnosis
  does not open on the pre-fault graph. Reaching the cap does not fail the
  attempt, because a fault that changes no watched object leaves no visible
  publish; diagnosis proceeds and the wait records `ended_by: "cap"`. The
  wait is recorded as `post_fault_wait` in `arm-native.json`;
  `scripts/benchmark/README.md` lists
  it with the other recorded differences between the arms.
  With `SREGYM_ARM` unset none of them runs.
  A third value, `SREGYM_ARM=edge` (sregym#147), is native minus the
  Kubernetes API connection: NOFire's production posture, where brain-go holds
  no Kubernetes API token at all. `seed_native_connections.py` seeds the
  three telemetry connections and drops `sregym-kubernetes`; no kubeconfig is
  read and `configmap_values` goes unused. `run_shard.sh` skips
  `native_arm.sh kubeconfig` and the API-server private-IP bind, both of
  which exist only for the Kubernetes connection, but still mints the edge
  key and installs the agent exactly as the native arm does.
  `setup_brain_go.sh` leaves `FEATURE_KUBERNETES_TOOLS_ENABLED` unset, so
  brain-go's `k8s_*` tool family stays off, the product default.
  `arm.py`'s `is_native` is true for both native and edge, since
  `native_gate.py`'s telemetry probe and empty-graph gate and the
  post-fault wait depend on the edge agent and the telemetry connections, not
  on the Kubernetes one; `is_edge` names the one difference. The run manifest
  is what records which arm actually ran: `write_run_manifest.sh` writes
  `arm=edge` and a new `kubernetes_connection` field (`mcp`, `native`, or
  `none`) alongside it.
  `build_mcp_image.sh` builds that server's image from the checkout and names
  it `sregym-mcp:<sregym sha>`; `run_shard.sh` builds once per box and loads it
  into each slot's kind node, and `write_run_manifest.sh` records the same name
  as the manifest's `mcp_image`. `run_shard.sh` holds
  the shard's resume, problem division and slot workers; it is sourced by the
  terraform bootstrap rather than executed, because user_data is capped at
  16384 bytes and `measure_user_data.py --check` is what keeps it under.
  Each slot loads its environment from `run_slot_env.sh` and its tenant from
  `seed_slot.sh` through `eval_exports`, which evals their `export` lines only
  when the script exited 0 and otherwise aborts the slot (sregym#125). The
  bootstrap starts its ssh-agent the same way, capturing `ssh-agent -s` before
  the eval so a failure reaches `terminate` (sregym#128).
  `tests/benchmark/test_eval_exports.py` fails on an `eval` in command position
  whose arguments hold a command substitution, `$(...)` or backticks, on one
  logical line of `scripts/benchmark/*.sh` or `terraform/templates/*.tftpl`.
  It skips comments, single-quoted text and escaped `\$(`, and joins
  backslash continuations. It does not follow a quoted string across lines or
  tell a heredoc body from code.
  `launch.sh` runs a named scenario from `terraform/<name>.tfvars` with both
  refs pinned to current `origin/main`.
- `terraform/` — the ephemeral spot fleet the suite runs on.

## Lessons carried over from the Python brain SREGym runbook

The original SREGym run against Python `brain` (unmerged,
[NOFireAI/brain#1119](https://github.com/NOFireAI/brain/pull/1119):
`scripts/sregym/RUNBOOK.md`, `run.sh`, `seed.sh`, `seed_connections.py`)
settled a few things the brain-go benchmark setup should keep rather than
rediscover:

- **Deploy a real in-cluster edge agent.** The Python run deployed
  `NOFireAI/edge` (`make helm-local` / `make docker-kind`) into the
  `monitoring` namespace, so the entity graph fed to brain came from the same
  live cluster the fault was injected into, not a fixture. Carried out under
  `SREGYM_ARM=native`, which installs the published `nofire-edge` chart into
  each slot's cluster in the `nofire-edge` namespace and checks that the agent
  has published before the slot's first problem.
- **RBAC exclusion for chaos-mesh is a correctness rule, not hygiene.** If the
  edge agent's ServiceAccount can read chaos-mesh CRDs
  (`podchaos.chaos-mesh.org` etc.), the injected fault shows up verbatim in
  the entity graph and the benchmark leaks its own ground truth to the agent
  under test. `native_arm.sh` runs `kubectl auth can-i list
  podchaos.chaos-mesh.org -A --as system:serviceaccount:<ns>:<sa>` against both
  the edge agent's ServiceAccount and the read-only one brain-go's Kubernetes
  connection uses, and aborts the slot on a `yes`.
- **MCP tool access already existed in the Python run — it is not new to
  brain-go.** `seed_connections.py` creates `Connection` rows with
  `kind=ConnectionKind.CUSTOM_MCP`, `service=DataSourceService.MCP`, pointing
  at the same four SREGym MCP endpoints brain-go seeds today
  (`sregym-kubectl` / `sregym-prometheus` / `sregym-jaeger` / `sregym-loki`,
  same bucket split: kubectl to `state`, telemetry to `observation`). What is
  new is not the mechanism, it is that brain-go's own tool-resolution path
  for CUSTOM_MCP tools was broken end to end
  ([NOFireAI/brain-go#2277](https://github.com/NOFireAI/brain-go/issues/2277)),
  which Python's differently-shaped resolution code never hit. Verified
  live 2026-08-26: with the fix, every seeded CUSTOM_MCP tool (including
  `exec_kubectl_cmd_safely`) resolves and executes; without it, every call
  failed `UnknownTool`.
- **`host.docker.internal:9954` is the correct MCP base address, not a
  workaround.** Python's `seed_connections.py` defaults `--mcp-base` to
  `http://host.docker.internal:9954`. brain-go's seeded connections had
  instead been pointed at a literal LAN IP, which went stale the moment the
  host's address changed (a VPN/Tailscale reconnect, observed 2026-08-26 as
  `dial tcp ...: i/o timeout` against the stale IP). Point at
  `host.docker.internal` from the start; it survives host IP churn that a
  literal IP does not.
- **Seed connections directly in the database, not over HTTP.** Python's
  `seed_connections.py` ran via `docker exec` inside the brain container and
  wrote the `Connection` table directly with SQLAlchemy, which is why it never
  hit brain's CSRF double-submit check. brain-go's
  `scripts/benchmark/seed_connections.py` currently seeds over `POST
  /api/connections` and works around brain-go's CSRF cookie (`Secure`, so no
  standards-compliant client sends it back over plain HTTP) by hand-attaching
  the cookie to the request — a workaround, not a fix. A direct-DB seed,
  matching brain-go's own `brainctl seed dev-stack`, avoids the problem
  entirely and is the better long-term shape.
- **`FORCE_RCI_FOR_CLI` semantics are validated.** Python's
  `force_rci_for_cli = cli_mode AND FORCE_RCI_FOR_CLI` routing to `rci` matches
  brain-go's port of the same behavior
  ([NOFireAI/brain-go#1698](https://github.com/NOFireAI/brain-go/issues/1698)).
- **brain-go does not need `claude_cli` as the LLM provider the way the Python
  runbook recommends.** Python preferred it over Bedrock because
  `AWS_BEARER_TOKEN_BEDROCK` expires in <=12h and botocore prefers a stale
  bearer token over valid SigV4 credentials, silently failing every call.
  brain-go uses the standard AWS credential chain (`LoadDefaultConfig`), not a
  bearer token, so this failure mode does not apply.

## Syncing from upstream

```bash
git remote add upstream git@github.com:SREGym/SREGym.git  # once per clone
git fetch upstream
git checkout upstream-main && git reset --hard upstream/main
git push --force-with-lease origin upstream-main   # allowed: only main is protected
git checkout main && git merge upstream-main
# resolve conflicts (our changes are deliberately small and env-gated),
# then push main
```

## Keeping the sync current

The mirror only tracks upstream if somebody makes it. Between 2026-07-29 and
2026-08-24 upstream moved six commits ahead, three of them in the last three
days, and one changed SREGym-Lite from 20 problems to 21. Check weekly: a small
gap is one conflicted function, a large one is a merge nobody wants to do.

Rehearse without touching the working tree:

```bash
git fetch upstream
git merge-tree --write-tree --name-only origin/main upstream/main
```

Keep the NOFire footprint in upstream files as small as possible — every change
upstream accepts (via a throwaway public fork + cherry-pick) is merge drift we
stop paying for. The brain driver itself stays private.
