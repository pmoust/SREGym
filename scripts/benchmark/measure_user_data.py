#!/usr/bin/env python3
"""Measure the bootstrap template against EC2's user_data cap.

This exists because the number kept going stale. A comment in
terraform/compute.tf carried the figure by hand and was wrong four times: it
was re-measured, the template grew in the next commit, and the comment stayed.
The last time, the stale number sat in the same comment that told the reader to
re-measure.

The cap is 16384 bytes on the base64 form, and it is enforced at
`terraform apply`, not at `plan`: user_data_base64 depends on local.run_id,
which derives from random_id and timestamp(), so its value is unknown until
apply. By then the bucket and security group exist. The failure looks like
"expected length of user_data to be in the range (0 - 16384)".

    scripts/benchmark/measure_user_data.py            # report
    scripts/benchmark/measure_user_data.py --check    # exit 1 if over

Exact bytes depend on deployment values (bucket name, brain_env, the SSM path),
so this reports a range across plausible inputs rather than one figure. The
pessimistic row is the one to budget against.
"""

import argparse
import base64
import gzip
import re
import sys
from pathlib import Path

CAP = 16384


# The real sregym-lite ids, so the pessimistic scenario measures real lengths.
# Read from the source of truth rather than copied, which would go stale the
# first time the set changes.
def _lite_ids() -> list[str]:
    src = (Path(__file__).resolve().parents[2] / "sregym" / "conductor" / "problem_sets.py").read_text()
    body = src.split("SREGYM_LITE_PROBLEMS = (")[1].split(")")[0]

    return re.findall(r'"([a-z0-9_]+)"', body)


TEMPLATE = Path(__file__).resolve().parents[2] / "terraform" / "templates" / "bootstrap.sh.tftpl"
_LITE_IDS = _lite_ids()

# Fixed-shape values: these have the same length in any real deployment.
FIXED = {
    "shard_index": "0",
    "shard_count": "3",
    "problem_set": "sregym-lite",
    "n_attempts": "8",
    "noise": "false",
    # Both arms interpolate to a fixed short string; "native" is the longer.
    "arm": "native",
    "agent_timeout_seconds": "1800",
    "runs_per_instance": "3",
    "instance_type": "m8i.8xlarge",
    "region": "us-east-2",
    "max_runtime_hours": "12",
    "keep_alive_on_failure": "false",
    "run_id": "run-20260901-1200-a4d231c1",
    "agent_model": "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
    "judge_model": "bedrock/anthropic.claude-sonnet-5-20260101-v1:0",
    "reasoning_effort": "medium",
    "sregym_ref": "0" * 40,
    "brain_go_ref": "0" * 40,
    # Account-derived, not run-derived (terraform/s3.tf), so unlike `bucket`
    # this is the same length in every real deployment and belongs in FIXED
    # rather than SCENARIOS.
    "registry_cache_bucket": "sregym-bench-registry-cache-819709950457",
}

# The values that actually vary in length, at both ends of what is plausible.
SCENARIOS = {
    "typical": {
        "bucket": "sregym-bench-run-20260901-1200-a4d231c1-results",
        "brain_env": "CHAT_SERVICE_TURN_TIMEOUT=3600s",
        "github_token_ssm_path": "/sregym/github-token",
        # The scored run leaves var.problems empty, so this interpolates to "".
        "problems": "",
    },
    "pessimistic": {
        "bucket": "sregym-bench-run-20261231-2359-deadbeef-results",
        "brain_env": (
            "CHAT_SERVICE_TURN_TIMEOUT=3600s\nRCI_MAX_HYPOTHESES=5\n"
            "RCI_TEMPERATURE=0.2\nFEATURE_TEMPLATED_PROMPT_LOGS=1"
        ),
        "github_token_ssm_path": "/nofire/sregym/benchmark/github-clone-token-readonly",
        # Every id named explicitly, which is the worst case a valid input can
        # reach: var.problems is validated against ^[a-z0-9_]+$ per entry, and
        # the set is 21 ids, so this bounds it. Real ids rather than padding,
        # because their length is the point: this string goes into user_data.
        "problems": " ".join(_LITE_IDS),
    },
}


def measure(template: str, values: dict[str, str]) -> tuple[int, int, int]:
    """Render, gzip and base64 the template the way base64gzip() does."""
    missing = set(re.findall(r"\$\{(\w+)\}", template)) - set(values)
    if missing:
        raise SystemExit(f"measure_user_data: template needs values not modelled here: {sorted(missing)}")

    rendered = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], template)
    packed = gzip.compress(rendered.encode(), 9)

    return len(rendered), len(packed), len(base64.b64encode(packed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if any scenario exceeds the cap")
    args = parser.parse_args()

    template = TEMPLATE.read_text()
    worst = 0
    for name, varying in SCENARIOS.items():
        rendered, packed, encoded = measure(template, {**FIXED, **varying})
        worst = max(worst, encoded)
        spare = CAP - encoded
        state = "OVER" if spare < 0 else "ok"
        print(
            f"{name:<12} rendered {rendered:>6}  gzip {packed:>6}  base64 {encoded:>6}"
            f"  spare {spare:>6}  {CAP / encoded:.3f}x  {state}"
        )

    if args.check and worst > CAP:
        print(f"\nFAIL: {worst - CAP} bytes over the {CAP} cap. Move shell into scripts/benchmark/.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
