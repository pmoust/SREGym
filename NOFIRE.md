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
- `main.py` — `SREGYM_SKIP_JUDGE_PREFLIGHT`, and it hardcodes the values it
  exports for `API_PORT` and `MCP_SERVER_PORT`. Parameterising those is what
  lets several runs share a host.
- `llm_backend/init_backend.py` — routes the judge to `ClaudeCliBackend` when
  `is_claude_cli_judge()` says so. Keep that branch when merging upstream: it
  conflicts with any upstream change to `get_llm_backend_for_judge`, and losing
  it fails silently by falling back to LiteLLM.
- `sregym/service/apps/astronomy_shop.py` — drops the flagd-ui sidecar and
  prunes two dead collector exporters. **Not env-gated**, so it applies to every
  run. Revert it for any run that claims to be stock.

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
