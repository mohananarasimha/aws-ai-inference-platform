# Architecture

One authenticated API in front of an agent that can answer from **Amazon Bedrock** or from a **self-hosted vLLM** model on EKS GPU nodes, with optional RAG, read-only tools over MCP, and human approval for write actions. This page is the map; the linked documents hold the detail and the evidence.

## Why two model paths

- **Bedrock (Nova)** is the managed path: nothing to run, pay per token, quotas and models controlled by AWS.
- **vLLM on EKS** is the self-hosted path: a model and runtime we control (`Qwen/Qwen2.5-7B-Instruct`, image `vllm/vllm-openai:v0.33.0`), on GPU capacity that bills while it exists. It exists to learn and measure what self-hosting costs in latency, cold start, recovery and operations.

A model adapter (`gateway/lambda/model.py`) gives both the same interface, so RAG, tools and approval work identically for either. The request field `provider` selects the path.

## Request path

```text
Client
  -> Cognito (client-credentials token)
  -> API Gateway HTTP API (JWT authorizer)
  -> Lambda agent host  (gateway/lambda/handler.py, agent.py)
       -> optional RAG: Bedrock Knowledge Base retrieve (rag.py)
       -> model provider: Bedrock converse   |   vLLM chat completions (model.py, vllm.py)
       -> optional MCP tools: AgentCore Runtime MCP server (mcp_client.py, mcp-agentcore/)
  -> response
```

One request deadline (24 s, below the 30 s Lambda and API Gateway limits) is shared by every downstream call: each gets `min(component maximum, time left)` (`deadline.py`, `config.py`). Known gap: [known-limitations.md](known-limitations.md#timeout-gap).

## Write and approval path

The model can only propose a change; it never executes one.

```text
agent proposes a write tool (update_lambda_memory)
  -> Step Functions approval workflow (callback task)
  -> message with task token on the approval SQS queue
  -> a human runs scripts/approve.py: shows the exact action, asks yes/no, answers the task
  -> approved: write-executor Lambda validates and applies the change; rejected: execution ends
```

Code: `approval.py`, `scripts/approve.py`, `write-executor/handler.py`, `terraform/environments/dev/app/approval.tf`, `write_executor.tf`.

## vLLM path

`provider=vllm` takes an optional `target` naming where the model runs (a logical name only, never a URL):

| Request | Route |
|---|---|
| `target` omitted or `"eks"` | EKS vLLM (the path below) |
| `target: "onprem"` | Lambda -> router (WireGuard) -> on-prem Nginx load balancer -> two on-prem vLLM replicas. The address is `ONPREM_VLLM_BASE_URL`, set by Terraform (`onprem_vllm_base_url`). |
| any other value | `400 invalid_target`; there is no fallback to EKS |

`VLLM_BASE_URL`, if set on the Lambda, overrides both (debugging). `target=onprem` with `ONPREM_VLLM_BASE_URL` unset is a `503 vllm_onprem_not_configured`. The on-prem route also needs the Lambda security group egress, the Lambda subnet route and the router security group to allow the on-prem port; those are not created by the `target` change.

The EKS path:

```text
Lambda (in the VPC; outbound through a self-managed NAT/router instance)
  -> runtime NLB discovery (vllm.py: resource tag service.k8s.aws/stack = default/vllm)
  -> internal NLB (created by the AWS Load Balancer Controller from the vLLM Service, IP targets)
  -> Kubernetes Service
  -> vLLM pod (readiness probe /health; Hugging Face cache and /dev/shm are emptyDir volumes)
  -> GPU
```

The Lambda finds the NLB hostname at runtime instead of Terraform wiring it in, so the NLB can be recreated without touching the app stack. The NLB has its own security group (`eks-addons/inference_nlb.tf`) allowing only the Lambda security group.

## EKS and GPU path

```text
vLLM pod Pending (requests nvidia.com/gpu, tolerates the GPU taint)
  -> Karpenter creates a NodeClaim from the GPU NodePool / EC2NodeClass
  -> g5.xlarge launches and joins the cluster
  -> NVIDIA device plugin advertises nvidia.com/gpu
  -> scheduler binds the pod; image pull; model load; readiness
```

The system node group runs the cluster add-ons and Karpenter; GPU nodes exist only while a GPU pod needs them. The GPU NodePool is limited to 4 CPU and 1 GPU. Measured timing of this path: [vllm-cold-start.md](vllm-cold-start.md).

## Observability path

```text
Lambda  -> structured logs + CloudWatch EMF metrics (metrics.py, namespace EnterpriseAIPlatform/Gateway)
vLLM /metrics, Kubernetes, DCGM GPU metrics
        -> CloudWatch Observability EKS add-on (OTel mode) -> CloudWatch (PromQL)
All of it -> one CloudWatch dashboard (terraform/environments/dev/app/dashboard.tf, 11 widgets)
```

Design and trade-offs: [decisions/observability.md](decisions/observability.md). Which widgets are verified: [known-limitations.md](known-limitations.md#observability).

## Terraform layout

Three stacks, each with its own local state and a plain `terraform apply`: `app` (Cognito, API, Lambda, RAG, AgentCore, approval, dashboard) then `eks` (cluster, system nodes) then `eks-addons` (Karpenter, GPU pool, device plugin, load balancer controller, CloudWatch add-on, vLLM manifests). They have different lifecycles and the Helm provider cannot depend cleanly on a cluster created in the same apply. Reasoning: [decisions/terraform-stacks.md](decisions/terraform-stacks.md). Deploy and teardown: `scripts/up.sh`, `scripts/down.sh`.

## Where the evidence is

| Topic | Document |
|---|---|
| Cold start (701 s, where it goes) | [vllm-cold-start.md](vllm-cold-start.md) |
| Pod and node failure recovery, quota finding | [experiments/vllm-failure-recovery.md](experiments/vllm-failure-recovery.md) |
| Why autoscaling is deferred | [decisions/vllm-cold-start-and-autoscaling.md](decisions/vllm-cold-start-and-autoscaling.md) |
| Active limitations and open issues | [known-limitations.md](known-limitations.md) |
