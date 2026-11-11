#!/usr/bin/env bash
# Tears the platform down in the safe order:
#
#   Kubernetes (Service, Deployment, GPU pool) -> eks-addons -> eks -> app
#
# The Service goes first and its load balancer must be gone before eks-addons
# is destroyed: the controller that owns the load balancer is removed with
# eks-addons, and a load balancer still using the inference security group
# blocks that group's deletion.
#
# Nothing asks for approval: the kubectl deletions run and every Terraform
# destroy uses -auto-approve. Use --dry-run first to see what would happen, or
# --confirm to be asked (one "yes" before the kubectl deletions, and Terraform's
# own prompt for each destroy).
#
#   scripts/down.sh                  everything
#   scripts/down.sh --keep-app       stop after eks (keep Cognito, API, Lambda, RAG)
#   scripts/down.sh --from eks       resume at a step (kubernetes|eks-addons|eks|app)
#   scripts/down.sh --dry-run        print what would run, change nothing
#   scripts/down.sh --confirm        ask before deleting, as Terraform normally does
#
# Works with the bash 3.2 that macOS ships.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DEV=$ROOT/terraform/environments/dev
MANIFESTS=$DEV/eks-addons/manifests
export AWS_PROFILE=${AWS_PROFILE:-personal}
export AWS_REGION=us-east-1

ACCOUNT=111122223333
NLB_SG_NAME=enterprise-ai-platform-dev-inference-nlb
STEPS="kubernetes eks-addons eks app"

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }
say() { echo; echo "==> $*"; }

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

from=kubernetes
keep_app=0
dry=0
confirm=0
while [ $# -gt 0 ]; do
  case $1 in
    --from)     [ -n "${2:-}" ] || die "--from needs a step: $STEPS"; from=$2; shift 2 ;;
    --keep-app) keep_app=1; shift ;;
    --dry-run)  dry=1; shift ;;
    --confirm)  confirm=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "unknown argument: $1 (see --help)" ;;
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

# wait_until "what" timeout_seconds interval_seconds check_function
# The check function returns 0 once the thing is gone. Returns 1 on timeout.
wait_until() {
  local what=$1 timeout=$2 interval=$3 check=$4 waited=0
  if [ "$dry" -eq 1 ]; then echo "  [dry-run] wait until $what"; return 0; fi
  echo "waiting for $what (up to ${timeout}s)"
  while ! "$check"; do
    if [ "$waited" -ge "$timeout" ]; then return 1; fi
    sleep "$interval"
    waited=$((waited + interval))
  done
  echo "  done: $what"
}

# ---------------------------------------------------------------- read-only checks
cluster_exists() {
  aws eks describe-cluster --name "$CLUSTER" >/dev/null 2>&1
}

# The gateway Lambda sits in a VPC. AWS releases its network interfaces in the
# background after the function is deleted (minutes, sometimes ~40), and the
# Lambda security group cannot be deleted until they are gone.
lambda_enis_gone() {
  [ -z "$(aws ec2 describe-network-interfaces \
    --filters "Name=description,Values=AWS Lambda VPC ENI-*-gateway" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null)" ]
}

vllm_nlb_gone() {
  [ -z "$(aws elbv2 describe-load-balancers \
    --query "LoadBalancers[?contains(LoadBalancerName, 'k8s-default-vllm')].LoadBalancerName" \
    --output text 2>/dev/null)" ]
}

vllm_target_groups_gone() {
  [ -z "$(aws elbv2 describe-target-groups \
    --query "TargetGroups[?contains(TargetGroupName, 'k8s-default-vllm')].TargetGroupName" \
    --output text 2>/dev/null)" ]
}

nlb_sg_free() {
  local sg
  sg=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$NLB_SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
  if [ -z "$sg" ] || [ "$sg" = "None" ]; then return 0; fi
  [ -z "$(aws ec2 describe-network-interfaces --filters "Name=group-id,Values=$sg" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null)" ]
}

controller_sg_gone() {
  [ -z "$(aws ec2 describe-security-groups --filters 'Name=group-name,Values=k8s-*' \
    --query 'SecurityGroups[].GroupId' --output text 2>/dev/null)" ]
}

no_ec2nodeclass() {
  ! kubectl get ec2nodeclass gpu >/dev/null 2>&1
}

no_nodeclaims() {
  [ -z "$(kubectl get nodeclaims -o name 2>/dev/null || true)" ]
}

no_gpu_instances() {
  [ -z "$(aws ec2 describe-instances \
    --filters 'Name=tag-key,Values=karpenter.sh/nodepool' \
              'Name=instance-state-name,Values=pending,running,stopping,shutting-down' \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null)" ]
}

no_karpenter_profiles() {
  [ -z "$(aws iam list-instance-profiles \
    --query "InstanceProfiles[?contains(InstanceProfileName, '$CLUSTER')].InstanceProfileName" \
    --output text 2>/dev/null)" ]
}

# Terraform-managed resources in a stack (data sources excluded). 0 if there is no state.
stack_resources() {
  (cd "$DEV/$1" && terraform state list 2>/dev/null | grep -vc '^data\.' || true) | tail -1
}

# ---------------------------------------------------------------- preflight
for tool in terraform kubectl aws; do
  command -v "$tool" >/dev/null || die "$tool is not installed or not on PATH"
done

actual=$(aws sts get-caller-identity --query Account --output text) \
  || die "AWS credentials for profile '$AWS_PROFILE' do not work (try: aws sso login --profile $AWS_PROFILE)"
[ "$actual" = "$ACCOUNT" ] || die "profile '$AWS_PROFILE' is account $actual, expected $ACCOUNT"

CLUSTER=${CLUSTER_NAME:-$(cd "$DEV/eks" && terraform output -raw eks_cluster_name 2>/dev/null || true)}
CLUSTER=${CLUSTER:-ai-inference-lab}

echo "Profile $AWS_PROFILE, account $actual, region $AWS_REGION, cluster $CLUSTER."
if [ "$dry" -eq 1 ]; then echo "(dry run: nothing will be changed)"; fi
if [ "$confirm" -eq 0 ]; then echo "Auto-approve is ON: resources are destroyed without asking (use --confirm to be asked)."; fi

# ---------------------------------------------------------------- Kubernetes
kubernetes_cleanup() {
  say "Kubernetes: Service, Deployment and GPU pool"

  if ! cluster_exists; then
    echo "cluster $CLUSTER not found: nothing to delete inside it"
    return
  fi

  run aws eks update-kubeconfig --name "$CLUSTER"

  if kubectl get svc vllm >/dev/null 2>&1 \
     && ! kubectl -n kube-system get deploy aws-load-balancer-controller >/dev/null 2>&1; then
    die "the vllm Service exists but the load balancer controller is gone, so its finalizer would hang.
Remove the finalizer yourself, then delete the load balancer and target group by hand:
  kubectl patch svc vllm --type=merge -p '{\"metadata\":{\"finalizers\":null}}'
  kubectl delete svc vllm
Then re-run: scripts/down.sh --from kubernetes"
  fi

  echo "This deletes the vLLM Service (and its load balancer), the vLLM Deployment,"
  echo "and the Karpenter GPU NodePool and EC2NodeClass (the GPU node terminates)."
  if [ "$dry" -eq 0 ] && [ "$confirm" -eq 1 ]; then
    read -r -p "Continue? Type yes: " answer
    [ "$answer" = "yes" ] || die "cancelled"
  fi

  # Service first: the controller removes the load balancer while it still exists.
  run kubectl delete svc vllm --ignore-not-found --timeout=300s
  wait_until "the vLLM load balancer to disappear" 300 10 vllm_nlb_gone \
    || die "the vLLM load balancer is still there after 5 minutes"
  wait_until "the vLLM target group to disappear" 120 10 vllm_target_groups_gone \
    || warn "a vLLM target group is still there; delete it by hand if it stays"

  run kubectl delete deploy vllm --ignore-not-found --timeout=300s
  run kubectl delete -f "$MANIFESTS/gpu-test-pod.yaml" -f "$MANIFESTS/vllm-pod.yaml" --ignore-not-found

  if kubectl get crd nodepools.karpenter.sh >/dev/null 2>&1; then
    # Karpenter drains the node, terminates the instance and deletes the instance
    # profile before it lets these go, which takes longer than kubectl is willing
    # to wait. Ask for the deletes, then wait on the real conditions below.
    run kubectl delete nodepool gpu --ignore-not-found --wait=false
    run kubectl delete ec2nodeclass gpu --ignore-not-found --wait=false
    wait_until "Karpenter node claims to disappear" 900 10 no_nodeclaims \
      || die "node claims are still present (check: kubectl get nodeclaims; kubectl -n kube-system logs deploy/karpenter)"
    wait_until "GPU instances to terminate" 600 10 no_gpu_instances \
      || die "a Karpenter-launched instance is still running"
    wait_until "the GPU EC2NodeClass to be removed" 600 10 no_ec2nodeclass \
      || die "ec2nodeclass gpu is still there (check: kubectl get ec2nodeclass gpu -o yaml; kubectl -n kube-system logs deploy/karpenter)"
    wait_until "Karpenter instance profiles to be removed" 180 10 no_karpenter_profiles \
      || warn "an instance profile for $CLUSTER is still there; destroying eks-addons may fail on the node role"
  else
    echo "no Karpenter CRDs: skipping the GPU pool"
  fi

  wait_until "the load balancer controller's security group to be removed" 180 10 controller_sg_gone \
    || warn "a k8s-* security group is left over; delete it by hand after the destroy"
  wait_until "network interfaces to release $NLB_SG_NAME" 300 10 nlb_sg_free \
    || die "network interfaces still use $NLB_SG_NAME; wait and re-run: scripts/down.sh --from eks-addons"
}

# ---------------------------------------------------------------- Terraform
tf_destroy() {
  local stack=$1 flag=""
  if [ "$confirm" -eq 0 ]; then flag="-auto-approve"; fi
  say "terraform destroy: $stack"
  if [ "$dry" -eq 1 ]; then
    echo "  [dry-run] (cd terraform/environments/dev/$stack && terraform init && terraform destroy $flag)"
    return
  fi
  (cd "$DEV/$stack" && terraform init -input=false >/dev/null && terraform destroy $flag)
}

require_gone() {
  local stack=$1 why=$2 count
  count=$(stack_resources "$stack")
  if [ "${count:-0}" -gt 0 ]; then
    if [ "$dry" -eq 1 ]; then
      echo "  [dry-run] $stack manages $count resources now; the steps above would destroy them ($why)"
    else
      die "$stack still manages $count resources; destroy it first ($why)"
    fi
  fi
}

if wanted kubernetes; then kubernetes_cleanup; fi

if wanted eks-addons; then
  if ! vllm_nlb_gone; then
    if [ "$dry" -eq 1 ]; then
      echo "  [dry-run] the vLLM load balancer exists now; the kubernetes step above would remove it"
    else
      die "the vLLM load balancer still exists; run the kubernetes step first"
    fi
  fi
  tf_destroy eks-addons
fi

if wanted eks; then
  require_gone eks-addons "eks-addons depends on the cluster"
  tf_destroy eks
fi

if wanted app; then
  if [ "$keep_app" -eq 1 ]; then
    say "keeping the app stack (--keep-app)"
  else
    require_gone eks-addons "its security group rule references the Lambda security group"
    if ! tf_destroy app; then
      say "app destroy failed; usually the Lambda security group still has a Lambda network interface"
      wait_until "the Lambda network interfaces to be released" 2700 30 lambda_enis_gone \
        || die "Lambda network interfaces are still there after 45 minutes; rerun scripts/down.sh --from app later"
      tf_destroy app
    fi
  fi
fi

# ---------------------------------------------------------------- what is left
say "left over (should all be empty after a full teardown)"
echo "EKS cluster:   $(aws eks describe-cluster --name "$CLUSTER" --query cluster.status --output text 2>/dev/null || echo none)"
echo "Load balancers: $(aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName' --output text 2>/dev/null)"
echo "GPU instances:  $(aws ec2 describe-instances --filters 'Name=tag-key,Values=karpenter.sh/nodepool' 'Name=instance-state-name,Values=pending,running' --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null)"
echo "k8s-* groups:   $(aws ec2 describe-security-groups --filters 'Name=group-name,Values=k8s-*' --query 'SecurityGroups[].GroupId' --output text 2>/dev/null)"
echo "(the manually built WireGuard router is not managed here and keeps running)"
