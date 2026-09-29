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
  injection (`SREGYM_BASELINE_SYNC_SECONDS`), and the native arm's gate at the
  end of that wait. The wait is a candidate for upstreaming; the gate is not,
  since it reads brain-go.
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
  with neither set the behaviour is unchanged.
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
  It also holds the native arm (`SREGYM_ARM=native`, brain-go#2410): `arm.py`
  resolves the flag, `seed_native_connections.py` registers brain-go's own
  Kubernetes, Prometheus, Loki and Jaeger connections in place of the four
  CUSTOM_MCP ones, `mint_edge_key.py` mints the edge-type key the in-cluster
  agent publishes with, `native_arm.sh` does the cluster-side work (API server
  address, read-only ServiceAccount, telemetry forwards, the Helm install),
  and `native_gate.py` plus `brain_graph.py` gate and record the entity graph.
  With `SREGYM_ARM` unset none of them runs.
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
  `tests/benchmark/test_eval_exports.py` fails on any `eval` of a command
  substitution in `scripts/benchmark/*.sh` or `terraform/templates/*.tftpl`.
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
  each slot's cluster in the `nofire-edge` namespace.
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
