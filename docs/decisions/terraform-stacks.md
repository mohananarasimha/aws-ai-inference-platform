# Decision: split Terraform into three stacks

Status: **implemented 2026-11-09 and in use.** Approved with one change: the security-group half of `inference_nlb.tf` lives in `eks-addons`.
Supersedes the earlier "stay flat in `terraform/environments/dev`" decision.

## Why

One state mixed layers with different lifecycles. The Helm provider was configured from a cluster created in the same apply, so builds needed ordered `-target` runs (a phase script and `docs/phases.md`, since deleted). That caused missing-dependency bugs (security group rules and IAM policies not reached by `-target`), a Helm webhook race, and teardowns that had to be hand-ordered.

Separate stacks, each with its own state and a plain `terraform apply`, remove `-target` from the normal workflow. All state was empty when this was decided, so no state migration is needed.

## Layout

```
terraform/environments/dev/
├── app/          Cognito, API Gateway, gateway Lambda, RAG, AgentCore, approval, Lambda security group
├── eks/          EKS cluster, system node group, add-ons, cluster and node IAM
└── eks-addons/   Karpenter, GPU NodePool, NVIDIA device plugin, AWS Load Balancer Controller, inference NLB security group, manifests/
```

Each stack has its own `backend.tf` (local state), `versions.tf`, `providers.tf`, `variables.tf`, `locals.tf`, `outputs.tf`, `terraform.tfvars.example`.

## File mapping (from the flat `dev/`)

| Stack | Files |
|---|---|
| `app` | `apigateway.tf` `cognito.tf` `lambda.tf` `iam.tf` `rag_s3.tf` `rag_iam.tf` `bedrock_knowledge_base.tf` `agentcore.tf` `agentcore_iam.tf` `approval.tf` `write_executor.tf` `network.tf` (the Lambda's security group and its rules towards the NAT/router; the NLB hostname is no longer looked up in Terraform, see below) |
| `eks` | `eks.tf` `eks_iam.tf` |
| `eks-addons` | `karpenter.tf` `karpenter_iam.tf` `gpu_nodepool.tf` `nvidia_device_plugin.tf` `aws_lbc.tf` `inference_nlb.tf` (NLB security group, its two rules, and the egress rule on the Lambda security group), `charts/karpenter-gpu/`, `policies/`, `manifests/` |

Split per stack: `versions.tf` (app: aws+archive, eks: aws, eks-addons: aws+helm), `providers.tf` (the helm provider goes to eks-addons only), `variables.tf`, `locals.tf` (3-line project/environment/name/tags block copied into each), `outputs.tf`.

Verified by reference analysis: `app` and `eks` reference nothing in any other stack. `eks-addons` references `eks` only through the cluster, its add-ons and its subnets.

## What crosses stack boundaries

| Need | Today | After |
|---|---|---|
| Cluster endpoint, CA, ARN, subnets, cluster SG | `aws_eks_cluster.main` | `data "aws_eks_cluster"` by name in eks-addons (also fixes the provider-from-same-apply problem) |
| EKS subnets for Karpenter discovery tags | `data.aws_subnets.eks` | `data.aws_eks_cluster.main.vpc_config[0].subnet_ids` |
| `ec2_assume` policy document, `eks_node_policy_arns`, `aws_caller_identity` | defined in `eks_iam.tf` / `iam.tf` | declared again in `eks-addons` (small, identical) |
| Lambda security group ID | `aws_security_group.gateway_lambda` | `app` outputs `lambda_security_group_id`; `eks-addons` reads it with `data.terraform_remote_state.app` (local state file) |
| Inference NLB security group | Terraform resource | lives in `eks-addons`; the Service finds it by `Name` tag, so no Terraform link to the Kubernetes side |
| NLB hostname for the Lambda | tag lookup | the Lambda discovers it at runtime (tag `service.k8s.aws/stack = default/vllm`, cached per container; `VLLM_BASE_URL` overrides). `app` only grants `tag:GetResources` and `elasticloadbalancing:DescribeLoadBalancers` and passes `VLLM_PORT`. No Terraform link to `eks-addons` |
| Shared inputs (`aws_region`, `allowed_account_ids`, `vpc_id`, `eks_cluster_name`, `gpu_instance_type`) | one `variables.tf` | repeated in the stacks that use them; keep defaults identical |

## Apply and destroy order

```
up:    app  ->  eks  ->  eks-addons  ->  kubectl apply manifests
down:  kubectl delete svc/deploy, nodepool, ec2nodeclass  ->  eks-addons  ->  eks  ->  app
```

- `app` runs **once**. It creates the Lambda security group and exports its ID. The Lambda finds the NLB itself when a vLLM request arrives, so there is no second run (changed 2026-11-09; the plan-time lookup needed one). Before the Service exists, vLLM requests return `vllm_endpoint_not_found` (503); Bedrock is unaffected. The discovery calls go through the router (NAT), like every other AWS API call from the Lambda.
- `eks-addons` refuses to plan until `app` has been applied once (`Unsupported attribute ... lambda_security_group_id`). That error is the ordering guard.
- Destroy `eks-addons` before `app`: the NLB security group's ingress rule references the Lambda security group, and AWS will not delete a security group that another group's rule references.

### Dependency graph (no cycles)

Edges: Terraform reference inside one stack (`-->`), exported output read through remote state (`=S=>`), plan-time AWS read with no Terraform link (`..R..>`), kubectl step (`=K=>`).

```
app                                                  eks              eks-addons
---                                                  ---              ----------
gateway_lambda SG --> https/vllm egress rules
   |  \--> output lambda_security_group_id ===S===>  remote_state.app --> inference_nlb ingress rule (from Lambda SG)
   v                                                      |            --> egress rule ON Lambda SG --> inference_nlb SG
aws_lambda_function.gateway <-- local.vllm_base_url       v                                                ^
                                      ^                data.aws_eks_cluster <-- eks cluster        inference_nlb SG
                                      :..R..                |                                             |
                          data.aws_lb.vllm <-- tag lookup   v                                             |
                                      ^            Karpenter, GPU pool, device plugin, LBC                |
                                      :...............  NLB  <=K=  Service vllm  <=K=  LBC ready + SG found by Name tag
```

Why there is no cycle: nothing in `app` depends on `eks-addons` through Terraform. The NLB hostname reaches the Lambda at runtime, through the AWS API, not through Terraform.

Prototype check (scratchpad, not in the repo): the two stacks, trimmed to this seam, both validate, report no cycle in `terraform graph`, and plan from nothing. `eks-addons` planned before `app` has an output fails with the expected "does not have an attribute" error.

## Path changes the move requires

- `app`: `lambda.tf`, `agentcore.tf` reference `${path.module}/../../../gateway/...` and `mcp-agentcore/...`; one directory deeper, so `../../../../`. The `.build/` folder moves with it.
- `eks-addons`: `charts/karpenter-gpu`, `policies/` and `manifests/` are referenced relative to the module and move with it.
- The `.claude/settings.json` deny rules and the "Terraform only runs in a folder with `backend.tf`" wrapper both still work (each stack has its own `backend.tf`).

## Outputs

`app`: lambda, invoke URL, Cognito, knowledge base, RAG, AgentCore, approval, write executor, `inference_nlb_security_group_id`. `eks`: cluster name and endpoint, kubeconfig command. `eks-addons`: `karpenter_node_role_name`. The `inference_nlb_subnet_ids` output is no longer needed (the Service lists its subnets directly).

## Leftovers in the flat `dev/` to decide on

`asd.json`, `tf.plan`, `terraform.tfstate`, `terraform.tfstate.backup`: not part of the layout. Ask before deleting (a saved plan can contain values).

## Cleanup

`scripts/up.sh` walks the stacks in the order above and applies the vLLM Deployment and Service (`--from STEP` to resume, `--dry-run`, `--wait`, `--retries N` for transient errors). `scripts/down.sh` (bash) reverses it: Service, Deployment and GPU pool first, waits until the load balancer and GPU nodes are gone, then `eks-addons`, `eks`, `app` (`--keep-app` stops before `app`).

The transitional phase tooling (`docs/phases.md`, `scripts/phase.zsh`) was deleted on 2026-11-09 after the first successful full run.
