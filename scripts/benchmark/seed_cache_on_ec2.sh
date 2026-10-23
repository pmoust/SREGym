#!/bin/bash
# Seed the registry cache from an EC2 instance, end to end, run from a
# developer laptop (NOFireAI/sregym#158). This is the default way to run
# seed_image_cache.sh and seed_host_images.sh: in-region, the first full seed
# took 16 minutes, against over an hour per attempt from a laptop.
#
# Scales the sregym-cache-seeder Auto Scaling group (NOFireAI/terraform,
# aws/sandbox/sregym/cache_seeder.tf) to 1, uploads seed_cache_remote.sh to the
# bucket, starts it on the instance through SSM, follows its log, and confirms
# the group is back at 0. The group is used instead of run-instances because
# DeveloperAccess can pass only the benchmark role to EC2.
#
# The instance ends itself (seed_cache_remote.sh's EXIT trap), so closing this
# script or the laptop after the seed has started does not leave it running.
# If this script fails BEFORE the seed starts, it scales the group back to 0.
#
# Usage: seed_cache_on_ec2.sh --sregym-ref REF --brain-go-ref REF [--brain-go-ref REF]...
#                              [--bucket NAME] [--region REGION]
#   --sregym-ref    the sregym_ref the run's tfvars pins: its image_cache.list
#                   is what gets seeded (default main)
#   --brain-go-ref  every brain_go_ref a run's tfvars pins; one host-image seed
#                   per ref. A full SHA, branch or tag; not an abbreviated SHA
#   --bucket        default sregym-bench-registry-cache-<caller's account id>
#   --region        default us-east-2
# Needs: aws CLI with credentials for the sandbox account (DeveloperAccess),
# python3. macOS-safe: no GNU-only tools.

set -euo pipefail

REGION=us-east-2
ASG=sregym-cache-seeder
BUCKET=""
SREGYM_REF=main
# The remote run is capped at 5 hours (seed_cache_remote.sh); stop following
# 30 minutes after that.
FOLLOW_LIMIT=$((5 * 3600 + 1800))
BRAIN_GO_REFS=()
HERE="$(cd "$(dirname "$0")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --sregym-ref) SREGYM_REF="$2"; shift 2 ;;
    --brain-go-ref) BRAIN_GO_REFS+=("$2"); shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ ${#BRAIN_GO_REFS[@]} -gt 0 ] || { echo "at least one --brain-go-ref is required" >&2; exit 2; }
for r in "${BRAIN_GO_REFS[@]}"; do
  # Same rule as terraform/variables.tf's brain_go_ref: a short SHA cannot be
  # fetched by itself, so reject one; a full SHA, branch or tag is fine.
  if [[ "$r" =~ ^[0-9a-f]{7,39}$ ]]; then
    echo "--brain-go-ref looks like an abbreviated SHA; use the full 40 characters: $r" >&2; exit 2
  fi
done

say() { echo "[seed $(date -u +%H:%M:%S)] $*"; }
asg_state() {
  aws autoscaling describe-auto-scaling-groups --region "$REGION" --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].[DesiredCapacity,length(Instances)]' --output text
}
wait_until() {  # wait_until <seconds> <description> <command...>
  local limit=$1 what=$2; shift 2
  local start=$SECONDS
  until "$@"; do
    [ $((SECONDS - start)) -lt "$limit" ] || { say "timed out after ${limit}s waiting for $what"; return 1; }
    sleep 10
  done
}

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
STARTED=0
cleanup() {
  local rc=$?
  if [ $STARTED -eq 0 ] && [ "${SCALED:-0}" -eq 1 ]; then
    say "failed before the seed started; scaling $ASG back to 0"
    aws autoscaling set-desired-capacity --region "$REGION" --auto-scaling-group-name "$ASG" --desired-capacity 0 || true
  fi
  exit $rc
}
trap cleanup EXIT

read -r ACCOUNT ARN <<<"$(aws sts get-caller-identity --query '[Account,Arn]' --output text)"
say "identity: $ARN"
[ -n "$BUCKET" ] || BUCKET="sregym-bench-registry-cache-$ACCOUNT"
PREFIX="s3://$BUCKET/seed-logs/$RUN_ID"
say "bucket: $BUCKET ($REGION), sregym@$SREGYM_REF, brain-go ${BRAIN_GO_REFS[*]}"
read -r desired count <<<"$(asg_state)"
[ "$desired" = 0 ] && [ "$count" = 0 ] || { say "$ASG is not idle (desired=$desired instances=$count); refusing to start a second seeder"; exit 1; }

say "run $RUN_ID: uploading seed_cache_remote.sh"
aws s3 cp "$HERE/seed_cache_remote.sh" "$PREFIX/seed_cache_remote.sh" --region "$REGION" --only-show-errors

say "scaling $ASG to 1"
aws autoscaling set-desired-capacity --region "$REGION" --auto-scaling-group-name "$ASG" --desired-capacity 1
SCALED=1

IID=""
in_service() {
  IID=$(aws autoscaling describe-auto-scaling-groups --region "$REGION" --auto-scaling-group-names "$ASG" \
    --query "AutoScalingGroups[0].Instances[?LifecycleState=='InService'].InstanceId | [0]" --output text)
  [ -n "$IID" ] && [ "$IID" != None ]
}
wait_until 600 "an InService instance" in_service
say "instance $IID is InService"

ssm_online() {
  [ "$(aws ssm describe-instance-information --region "$REGION" --filters "Key=InstanceIds,Values=$IID" \
    --query 'InstanceInformationList[0].PingStatus' --output text)" = Online ]
}
wait_until 600 "SSM to come online on $IID" ssm_online
say "SSM is online"

PARAMS=$(python3 - "$BUCKET" "$REGION" "$RUN_ID" "$SREGYM_REF" "${BRAIN_GO_REFS[@]}" <<'PY'
import json, shlex, sys
bucket, region, run_id, sregym_ref, *refs = sys.argv[1:]
prefix = f"s3://{bucket}/seed-logs/{run_id}"
args = " ".join(shlex.quote(a) for a in [bucket, region, run_id, sregym_ref, *refs])
cmds = [
    "set -e",
    "export DEBIAN_FRONTEND=noninteractive",
    "command -v aws >/dev/null || { apt-get update -y -q && apt-get install -y -q unzip curl >/dev/null"
    " && curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip"
    " && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install >/dev/null; }",
    f"aws s3 cp {shlex.quote(prefix + '/seed_cache_remote.sh')} /root/seed_cache_remote.sh --region {shlex.quote(region)} --only-show-errors",
    f"nohup bash /root/seed_cache_remote.sh {args} >/dev/null 2>&1 &",
    "echo started",
]
print(json.dumps({"commands": cmds, "executionTimeout": ["600"]}))
PY
)

say "starting seed_cache_remote.sh through SSM"
CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$IID" \
  --document-name AWS-RunShellScript --comment "sregym cache seed $RUN_ID" \
  --parameters "$PARAMS" --query Command.CommandId --output text)
launched() {
  local s
  s=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$IID" \
    --query Status --output text 2>/dev/null || echo Pending)
  case "$s" in
    Success) return 0 ;;
    Failed|Cancelled|TimedOut) say "launch command $s"; aws ssm get-command-invocation --region "$REGION" \
      --command-id "$CMD_ID" --instance-id "$IID" --query '[StandardOutputContent,StandardErrorContent]' --output text; exit 1 ;;
    *) return 1 ;;
  esac
}
wait_until 600 "the launch command" launched
STARTED=1
say "seed started on $IID. It ends itself when done; Ctrl-C here only stops following."
say "log: $PREFIX/seed.log"

TMP=$(mktemp -d)
seen=0
follow_start=$SECONDS
while :; do
  if aws s3 cp "$PREFIX/seed.log" "$TMP/seed.log" --region "$REGION" --only-show-errors 2>/dev/null; then
    total=$(wc -l <"$TMP/seed.log" | tr -d ' ')
    if [ "$total" -gt "$seen" ]; then
      tail -n +"$((seen + 1))" "$TMP/seed.log" | grep -E '^\[remote-seed|^\[seed-|exit=|MISSING|ERROR' || true
      seen=$total
    fi
  fi
  if aws s3 cp "$PREFIX/status" "$TMP/status" --region "$REGION" --only-show-errors 2>/dev/null; then
    break
  fi
  # An instance that dies before it can upload status (e.g. the AWS CLI never
  # installed) shuts down instead of leaving the group, and the group launches
  # an idle replacement. Stop waiting past the remote cap and scale to 0.
  if [ $((SECONDS - follow_start)) -ge "$FOLLOW_LIMIT" ]; then
    say "no status after ${FOLLOW_LIMIT}s; scaling $ASG to 0"
    aws autoscaling set-desired-capacity --region "$REGION" --auto-scaling-group-name "$ASG" --desired-capacity 0
    exit 1
  fi
  sleep 30
done

say "seed finished:"
sed 's/^/  /' "$TMP/status"
aws s3 cp "$PREFIX/seed.log" "$TMP/seed.log" --region "$REGION" --only-show-errors || true
missing=$(grep -c 'MISSING from the bucket' "$TMP/seed.log" || true)
say "MISSING lines in the read-back check: $missing"

group_empty() { local d n; read -r d n <<<"$(asg_state)"; [ "$d" = 0 ] && [ "$n" = 0 ]; }
if wait_until 600 "$ASG to return to 0" group_empty; then
  say "$ASG is back at 0, no instances"
else
  say "instance did not leave; scaling $ASG to 0"
  aws autoscaling set-desired-capacity --region "$REGION" --auto-scaling-group-name "$ASG" --desired-capacity 0
fi

grep -q '^overall=0$' "$TMP/status" && [ "$missing" = 0 ]
