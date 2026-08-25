# Benchmark fleet

Ephemeral EC2 for running the SREGym suite against brain-go. Spawn, run,
terminate. Nothing here is meant to outlive a run.

Tracked by NOFireAI/brain-go#2218, under NOFireAI/engineering#1411.

## Why it lives here

It provisions for this harness and follows its shape, so it sits beside it
rather than in `NOFireAI/terraform`, which holds standing environments. Revisit
if a dedicated benchmark repo appears, or once we run slices of the suite rather
than whole sweeps.

## Prerequisites

One-time, per account:

```bash
# A token that can clone both this private mirror and NOFireAI/brain-go
aws ssm put-parameter --region us-east-1 \
  --name /sregym/benchmark/github-token \
  --type SecureString --value "ghp_..."
```

Bedrock model access must be enabled in the region for both the agent and judge
models.

## Running

```bash
terraform init

# One box, one attempt per problem, no noise. The first sweep.
terraform apply -var instance_count=1

# The paper's protocol on Lite: 21 problems x 3 attempts x 2 noise conditions.
terraform apply -var instance_count=2 -var n_attempts=3 -var noise=true
```

Watch it, and check progress:

```bash
terraform output watch      # prints an SSM command that tails the bootstrap log
terraform output progress   # prints an S3 command that counts finished problems
```

Instances terminate themselves when their shard finishes. `terraform destroy`
then removes the bucket, role and security group. It does not need to remove the
instances, and if you run it early it will.

## How the pieces fit

**Sharding is static**: problem index modulo `instance_count`. No queue and no
coordinator, which is the right trade for 90 to 540 runs where the only failure
mode that matters is losing an instance.

**Spot, and losing one is cheap.** `main.py --resume` counts attempts per
`problem_id` and skips any problem that already has `n_attempts` rows, and each
problem's CSV is written as it completes. The bootstrap syncs `results/` to S3
after every problem and pulls it back on boot, so a replacement resumes where the
dead instance stopped. Losing a box costs the in-flight problem.

To replace a killed instance, `terraform apply` again.

**It terminates itself twice over.** Once when the shard completes, and again if
`max_runtime_hours` elapses. Both matter: an `r7a.24xlarge` is about $7.30/hour
on demand, and the failure that actually costs money is a forgotten box, not a
failed run.

**Access is SSM Session Manager**, so the security group has no inbound rules at
all. No keys, no bastion, and sessions are audited.

## Choices worth knowing

**gp3, 500 GB, for the IOPS rather than the capacity.** gp2 scales IOPS with size
at 3/GB, so 200 GB of gp2 is 600 IOPS and throttles concurrent image pulls. gp3
gives 3000 IOPS and 125 MB/s regardless of size, tunable independently. Sized
generously because every kind node keeps its own image cache: ENOSPC is what
kills the smoke-test CI today, and it surfaces as `ErrImagePull` rather than as a
disk error.

**The judge model must differ from the agent model**, enforced by a
precondition. A model grading its own output is not a measurement. Note that two
*cheap* judges agreeing is also weak evidence: SREGym's own inter-rater table has
GPT-5.4 and K2.5 agreeing with each other at kappa 0.94 while agreeing with a
human expert at only 0.70 and 0.76.

**Every `SREGYM_*` intervention is off.** They exist to work around a 24 GB ARM
laptop and they are why engineering#799's numbers cannot be read. `SETTLE_SECONDS`
defaults to 180, so the bootstrap sets it to `0` explicitly rather than leaving it
unset.

**`runs_per_instance` is pinned at 1.** The harness hardcodes its host ports:
9954 for the MCP server, 9090 and 3100 for the observe port-forwards, 16443 for
the k8s proxy, 8000 for the conductor API. Two harness processes cannot share a
host until NOFireAI/brain-go#2219 parameterises them. One cluster per run is a
hard constraint; one host per run is not.

## brain-go

`scripts/benchmark/setup_brain_go.sh` stands up brain-go on the box, seeds the
account and the four CUSTOM_MCP connection rows, and mints the CLI key
(NOFireAI/brain-go#2219). The bootstrap clones brain-go at `brain_go_ref`
(default `main`) with the same token as the sregym checkout, then runs the
script; a missing or failed clone exits loudly rather than burning an hour
looking healthy. `BRAIN_API_URL` defaults to `localhost:8001`, so brain-go runs
on the box beside the harness rather than centrally.

The MCP server is addressed by the instance's RFC1918 address with
`allow_private_endpoint` set per connection row, because brain-go's SSRF guard
blocks loopback with no flag that lifts it, and that allowance is honoured
only on the in-process CUSTOM_MCP path, not the Temporal MCP activity path.

## Cost

Compute is the cheap half. `r7a.24xlarge` is about $7.30/hour on demand and has
been running near $1.43 spot in us-east-1. A full 540-run protocol is a few tens
of dollars of compute. The token spend is the number to watch, and what to
capture from the first run is in NOFireAI/brain-go#2219.
