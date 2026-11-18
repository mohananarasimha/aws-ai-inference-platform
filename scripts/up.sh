#!/usr/bin/env bash
# Creates the whole platform, in order, with plain `terraform apply` (no -target):
#
#   app -> eks -> eks-addons -> vLLM Deployment and Service (kubectl)
#
# Every Terraform apply stops at Terraform's own "Enter a value: yes" prompt
# after showing its plan. Nothing here approves anything for you.
#
#   scripts/up.sh                     everything
#   scripts/up.sh --from eks-addons   resume at a step (app|eks|eks-addons|manifests)
#   scripts/up.sh --wait              also wait until the vLLM pod is ready
#   scripts/up.sh --dry-run           print what would run, change nothing
#   scripts/up.sh --retries 4         retries per apply after a transient failure (default 2)
#   scripts/up.sh --auto-approve-retries   retries skip the yes prompt (default: they ask again)
#
# A failed apply is retried only when the error looks transient (Helm chart
# download, TLS handshake, timeouts, throttling). Any other error stops the script.
# A Helm release that timed out is tainted, so the retry replaces it.
#
# Safe to run again: applied stacks report no changes and kubectl apply is idempotent.
# Works with the bash 3.2 that macOS ships.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DEV=$ROOT/terraform/environments/dev
MANIFESTS=$DEV/eks-addons/manifests
# Optional local settings (git-ignored), see .env.example.
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
export AWS_PROFILE=${AWS_PROFILE:-default}
export AWS_REGION=us-east-1

STEPS="app eks eks-addons manifests"

die() { echo "error: $*" >&2; exit 1; }

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

from=app
wait=0
dry=0
retries=2
auto_retry=0
while [ $# -gt 0 ]; do
  case $1 in
    --from)    [ -n "${2:-}" ] || die "--from needs a step: $STEPS"; from=$2; shift 2 ;;
    --wait)    wait=1; shift ;;
    --dry-run) dry=1; shift ;;
    --retries) [[ ${2:-} =~ ^[0-9]+$ ]] || die "--retries needs a number"; retries=$2; shift 2 ;;
    --auto-approve-retries) auto_retry=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         die "unknown argument: $1 (see --help)" ;;
  esac
done

step_index() {
  local i=0 s
  for s in $STEPS; do
    if [ "$s" = "$1" ]; then echo $i; return; fi
    i=$((i + 1))
  done
  echo -1
}

[ "$(step_index "$from")" -ge 0 ] || die "unknown step '$from' (use: $STEPS)"

wanted() { [ "$(step_index "$1")" -ge "$(step_index "$from")" ]; }

# Runs a command, or only prints it with --dry-run.
run() {
  if [ "$dry" -eq 1 ]; then echo "  [dry-run] $*"; else "$@"; fi
}

# Errors worth another try: network and throttling, not configuration mistakes.
TRANSIENT='TLS handshake timeout|context deadline exceeded|i/o timeout|Client\.Timeout|connection reset by peer|unexpected EOF|Unable to locate chart|failed to download|failed to pull|timed out waiting for the condition|Service Unavailable|RequestLimitExceeded|Throttling'

tf_apply() {
  local stack=$1 attempt=1 max=$((retries + 1)) backoff=${RETRY_BASE_SECONDS:-30} extra="" log rc
  echo; echo "==> terraform apply: $stack"
  if [ "$dry" -eq 1 ]; then
    echo "  [dry-run] (cd terraform/environments/dev/$stack && terraform init && terraform apply)  # up to $max attempts on transient errors"
    return
  fi

  while true; do
    log=$(mktemp)
    # tee keeps Terraform's prompt working and lets us read the error afterwards.
    set +e
    (cd "$DEV/$stack" && terraform init -input=false >/dev/null && terraform apply $extra) 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    set -e

    if [ "$rc" -eq 0 ]; then rm -f "$log"; return 0; fi

    if [ "$attempt" -ge "$max" ] || ! grep -qE "$TRANSIENT" "$log"; then
      rm -f "$log"
      if [ "$attempt" -ge "$max" ] && [ "$attempt" -gt 1 ]; then
        die "terraform apply of $stack failed after $attempt attempts"
      fi
      die "terraform apply of $stack failed (not a transient error, or retries are off)"
    fi

    rm -f "$log"
    echo; echo "!! $stack: transient error (attempt $attempt of $max). Retrying in $((attempt * backoff))s."
    sleep $((attempt * backoff))
    attempt=$((attempt + 1))
    if [ "$auto_retry" -eq 1 ]; then extra="-auto-approve"; fi
  done
}

cluster_name() { (cd "$DEV/eks" && terraform output -raw eks_cluster_name); }

nlb_subnets() {
  if [ -n "${VLLM_NLB_SUBNETS:-}" ]; then echo "$VLLM_NLB_SUBNETS"; return; fi
  aws eks describe-cluster --name "$(cluster_name)" \
    --query 'cluster.resourcesVpcConfig.subnetIds' --output text | tr '\t' ','
}

ensure_kubeconfig() {
  local name
  if ! name=$(cluster_name 2>/dev/null); then
    if [ "$dry" -eq 1 ]; then
      name="<cluster>"
    else
      die "no eks_cluster_name output: has the eks stack been applied?"
    fi
  fi
  echo; echo "==> kubeconfig for $name"
  run aws eks update-kubeconfig --name "$name"
}

# ---------------------------------------------------------------- preflight
for tool in terraform kubectl aws; do
  command -v "$tool" >/dev/null || die "$tool is not installed or not on PATH"
done

for file in vllm-deploy.yaml vllm-service.yaml; do
  [ -f "$MANIFESTS/$file" ] || die "missing $MANIFESTS/$file"
done

# The only account this script may touch: EXPECTED_AWS_ACCOUNT_ID, otherwise the
# first allowed_account_ids entry in the app stack's local terraform.tfvars.
expected_account() {
  if [ -n "${EXPECTED_AWS_ACCOUNT_ID:-}" ]; then echo "$EXPECTED_AWS_ACCOUNT_ID"; return; fi
  sed -n 's/^allowed_account_ids *= *\["\([0-9]\{12\}\)".*/\1/p' "$DEV/app/terraform.tfvars" 2>/dev/null | head -1
}
ACCOUNT=$(expected_account)
[ -n "$ACCOUNT" ] || die "set EXPECTED_AWS_ACCOUNT_ID (see .env.example) or allowed_account_ids in $DEV/app/terraform.tfvars"

actual=$(aws sts get-caller-identity --query Account --output text) \
  || die "AWS credentials for profile '$AWS_PROFILE' do not work (try: aws sso login --profile $AWS_PROFILE)"
[ "$actual" = "$ACCOUNT" ] || die "profile '$AWS_PROFILE' is account $actual, expected $ACCOUNT"

planned=""
for s in $STEPS; do
  if wanted "$s"; then planned="$planned $s"; fi
done
echo "Profile $AWS_PROFILE, account $actual, region $AWS_REGION. Steps:$planned"
if [ "$dry" -eq 1 ]; then echo "(dry run: nothing will be changed)"; fi

# ---------------------------------------------------------------- steps
if wanted app; then tf_apply app; fi

if wanted eks; then tf_apply eks; fi

if wanted eks-addons; then
  ensure_kubeconfig
  tf_apply eks-addons
fi

if wanted manifests; then
  if ! wanted eks-addons; then ensure_kubeconfig; fi

  echo; echo "==> waiting for the AWS Load Balancer Controller"
  run kubectl -n kube-system rollout status deploy/aws-load-balancer-controller --timeout=300s

  echo; echo "==> applying the vLLM Deployment and Service"
  run kubectl apply -f "$MANIFESTS/vllm-deploy.yaml"
  # The Service YAML holds a ${VLLM_NLB_SUBNETS} placeholder: the NLB goes in the
  # cluster's own subnets (the default-VPC subnets in the AZs that offer the GPU type).
  if ! subnets=$(nlb_subnets 2>/dev/null) || [ -z "$subnets" ]; then
    if [ "$dry" -eq 1 ]; then subnets="<cluster-subnets>"; else die "cannot read the cluster subnets; set VLLM_NLB_SUBNETS"; fi
  fi
  rendered=$(mktemp "${TMPDIR:-/tmp}/vllm-service.XXXXXX")
  sed "s|\${VLLM_NLB_SUBNETS}|$subnets|" "$MANIFESTS/vllm-service.yaml" > "$rendered"
  echo "  NLB subnets: $subnets"
  run kubectl apply -f "$rendered"
  rm -f "$rendered"

  if [ "$wait" -eq 1 ]; then
    echo; echo "==> waiting for vLLM (new GPU node, image pull, model load: can take 10+ minutes)"
    run kubectl rollout status deploy/vllm --timeout=30m
  fi
fi

# ---------------------------------------------------------------- done
echo
echo "Done. Useful next:"
echo "  kubectl get pods -l app=vllm -w          # GPU node, image pull, model load"
echo "  kubectl get svc vllm                     # internal NLB hostname appears here"
echo "  (cd terraform/environments/dev/app && terraform output invoke_url)"
