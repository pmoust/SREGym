#!/bin/bash
# Runs ON the seeder instance (sregym-cache-seeder group), started by
# seed_cache_on_ec2.sh through SSM; not meant to be run by hand. Installs the
# tools, clones sregym and brain-go, runs both seed scripts, and always ends by
# leaving the group.
#
# Usage: seed_cache_remote.sh <bucket> <region> <run-id> <sregym-ref> <brain-go-ref>...
#
# The instance ends itself whatever happens: the EXIT trap uploads the log and
# a status file, then leaves the group lowering desired capacity, and falls
# back to shutdown (the launch template terminates on shutdown). The whole run
# is capped by `timeout` so a hung seed cannot keep the box alive.

set -uo pipefail

BUCKET="$1"; REGION="$2"; RUN_ID="$3"; SREGYM_REF="$4"; shift 4
BRAIN_GO_REFS=("$@")
LOG=/root/seed.log
PREFIX="s3://$BUCKET/seed-logs/$RUN_ID"
MAX_SECONDS=$((5 * 3600))
STATUS=/root/seed.status
: >"$STATUS"

say() { echo "[remote-seed $(date -u +%H:%M:%S)] $*"; }

leave() {
  local rc=$?
  say "exiting (rc=$rc); uploading log and leaving the group"
  echo "overall=$rc" >>"$STATUS"
  [ -n "${UPLOADER_PID:-}" ] && kill "$UPLOADER_PID" 2>/dev/null
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "$LOG" "$PREFIX/seed.log" --region "$REGION" --only-show-errors
    aws s3 cp "$STATUS" "$PREFIX/status" --region "$REGION" --only-show-errors
    local t iid
    t=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
    iid=$(curl -sf -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/instance-id)
    aws autoscaling terminate-instance-in-auto-scaling-group --region "$REGION" \
      --instance-id "$iid" --should-decrement-desired-capacity && exit "$rc"
  fi
  say "could not leave the group through the API; shutting down"
  shutdown -h now
}
trap leave EXIT

main() {
  export DEBIAN_FRONTEND=noninteractive
  say "installing tools"
  apt-get update -y -q && apt-get install -y -q docker.io docker-compose-v2 git unzip curl python3 >/dev/null || return 10
  systemctl start docker || return 11
  curl -fsSL https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz \
    | tar -xz -C /usr/local/bin crane || return 12
  say "$(crane version 2>&1) / $(aws --version 2>&1) / $(docker --version) / $(docker compose version)"
  say "identity: $(aws sts get-caller-identity --query Arn --output text)"

  say "cloning sregym@$SREGYM_REF and brain-go"
  local token
  token=$(aws ssm get-parameter --region "$REGION" --name /sregym/benchmark/github-token \
    --with-decryption --query Parameter.Value --output text) || return 20
  cd /root || return 21
  git clone -q "https://x-access-token:${token}@github.com/NOFireAI/sregym.git" || return 22
  git -C sregym checkout -q "$SREGYM_REF" || return 23
  git clone -q --filter=blob:none "https://x-access-token:${token}@github.com/NOFireAI/brain-go.git" || return 24
  git -C brain-go fetch -q origin "${BRAIN_GO_REFS[@]}" || return 25
  unset token
  say "sregym at $(git -C sregym log --oneline -1)"

  cd /root/sregym || return 26
  # The registry containers run on docker's bridge network and get no
  # credentials from IMDS there (NoCredentialProviders on the first try, even
  # with hop limit 2). Hand them the instance role's temporary keys as plain
  # environment variables instead; registry_mirror.sh passes AWS_* through.
  unset AWS_PROFILE
  local imds_t role creds
  imds_t=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300') || return 27
  role=$(curl -sf -H "X-aws-ec2-metadata-token: $imds_t" http://169.254.169.254/latest/meta-data/iam/security-credentials/) || return 28
  creds=$(curl -sf -H "X-aws-ec2-metadata-token: $imds_t" "http://169.254.169.254/latest/meta-data/iam/security-credentials/$role") || return 29
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  AWS_ACCESS_KEY_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])' <<<"$creds")
  AWS_SECRET_ACCESS_KEY=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretAccessKey"])' <<<"$creds")
  AWS_SESSION_TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Token"])' <<<"$creds")
  say "using $role's temporary keys, expiring $(python3 -c 'import json,sys; print(json.load(sys.stdin)["Expiration"])' <<<"$creds")"
  unset creds
  local failed=0 rc

  say "seeding the image cache"
  scripts/benchmark/seed_image_cache.sh --bucket "$BUCKET" --region "$REGION"
  rc=$?; echo "image_cache=$rc" >>"$STATUS"; [ $rc -eq 0 ] || failed=1

  for ref in "${BRAIN_GO_REFS[@]}"; do
    say "seeding host images for brain-go@$ref"
    scripts/benchmark/seed_host_images.sh --bucket "$BUCKET" --region "$REGION" \
      --brain-go-repo /root/brain-go --brain-go-ref "$ref"
    rc=$?; echo "host_images_${ref:0:8}=$rc" >>"$STATUS"; [ $rc -eq 0 ] || failed=1
  done
  return $failed
}

exec >>"$LOG" 2>&1
say "run $RUN_ID starting, cap ${MAX_SECONDS}s"

# The AWS CLI is needed for the trap and the log uploader, so it goes first.
if ! command -v aws >/dev/null 2>&1; then
  apt-get update -y -q && apt-get install -y -q unzip curl >/dev/null
  curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install >/dev/null
fi

# Push the log every 60s so the laptop can follow progress.
( while sleep 60; do aws s3 cp "$LOG" "$PREFIX/seed.log" --region "$REGION" --only-show-errors; done ) &
UPLOADER_PID=$!

export -f main say
export BUCKET REGION SREGYM_REF STATUS
BRAIN_GO_REFS_STR="${BRAIN_GO_REFS[*]}"
export BRAIN_GO_REFS_STR
timeout --kill-after=60 "$MAX_SECONDS" bash -c 'BRAIN_GO_REFS=($BRAIN_GO_REFS_STR); main'
rc=$?
[ $rc -eq 124 ] && say "hit the ${MAX_SECONDS}s cap"
exit $rc
