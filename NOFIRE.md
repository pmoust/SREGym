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

Keep the NOFire footprint in upstream files as small as possible — every change
upstream accepts (via a throwaway public fork + cherry-pick) is merge drift we
stop paying for. The brain driver itself stays private.
