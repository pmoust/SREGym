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
  injection (`SREGYM_BASELINE_SYNC_SECONDS`). Candidate for upstreaming.
- `agents.yaml` — `brain` agent entry; `container_isolation: false` on
  host-run agents.
- `main.py` — `SREGYM_SKIP_JUDGE_PREFLIGHT`, and it sets `API_PORT`,
  `MCP_SERVER_PORT` and friends as defaults rather than overrides so several
  runs can share a host. Candidate for upstreaming.
- `llm_backend/init_backend.py` — routes the judge to `ClaudeCliBackend` when
  `is_claude_cli_judge()` says so. Keep that branch when merging upstream: it
  conflicts with any upstream change to `get_llm_backend_for_judge`, and losing
  it fails silently by falling back to LiteLLM.
- `sregym/service/apps/astronomy_shop.py` — drops the flagd-ui sidecar and
  prunes two dead collector exporters. **Not env-gated**, so it applies to every
  run. Revert it for any run that claims to be stock.
- `sregym/service/mcp_server.py` — the host side of the MCP port-forward reads
  `MCP_SERVER_PORT` instead of hardcoding 9954. Candidate for upstreaming.
- `sregym/paths.py` — `SREGYM_CACHE_DIR` overrides the cache location, so runs
  sharing a host do not share a per-cluster baseline snapshot. Candidate for
  upstreaming.
- `kind/setup_kind_cluster.sh` — `KIND_CLUSTER_NAME`, so a second cluster on the
  same host does not collide with the default name `kind`. Candidate for
  upstreaming.

NOFire-only directories, which add no merge drift because upstream has no such
path:

- `clients/brain_go/` — the brain-go agent driver and its SSE parser, with tests
  in `tests/brain_go/`.
- `scripts/benchmark/` — stands up and seeds brain-go on an instance
  (`setup_brain_go.sh`, `seed_connections.py`) and computes the per-run
  environment that lets runs share a host (`run_slot_env.sh`).
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
  live cluster the fault was injected into, not a fixture. brain-go's
  benchmark setup should do the same rather than seed the graph by hand.
- **RBAC exclusion for chaos-mesh is a correctness rule, not hygiene.** If the
  edge agent's ServiceAccount can read chaos-mesh CRDs
  (`podchaos.chaos-mesh.org` etc.), the injected fault shows up verbatim in
  the entity graph and the benchmark leaks its own ground truth to the agent
  under test. Verify with `kubectl auth can-i list podchaos.chaos-mesh.org -A
  --as system:serviceaccount:monitoring:nofire-edge` — it must print `no`.
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
