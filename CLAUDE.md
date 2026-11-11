# Enterprise AI Platform

## Goal

Build a production-style enterprise AI platform on AWS that allows internal applications to consume AI capabilities safely, observably, reliably, and cost-effectively.

This is primarily an infrastructure/platform engineering project, not a large application-development project.


## Project direction

Later phases may include:
- multiple Bedrock models
- model routing
- quotas / rate limits
- RAG
- MCP
- infrastructure agent
- approval-required actions
- AI observability
- vLLM on EKS
- GPU infrastructure
- autoscaling
- reliability testing

Do not implement future phases unless explicitly asked.

EKS, Karpenter, GPU scheduling, vLLM and the internal inference NLB were explicitly requested and are in scope.

## Engineering principles

- Make small, understandable changes.
- Do not redesign the repository without justification.
- Do not add technologies because they are popular.
- Explain why every new AWS resource exists.
- Prefer least-privilege IAM.
- Do not use AdministratorAccess.
- Do not hardcode AWS credentials.
- Do not create unnecessary VPC/networking resources.
- Do not create unnecessary abstractions or Terraform modules.
- Keep application code minimal.
- Infrastructure behavior must remain visible and understandable.

## Terraform rules


Terraform is the source of truth for infrastructure.

Before adding Terraform:
1. Inspect existing files.
2. Explain what resources will be added.
3. Map each resource to the architecture.
4. Keep changes scoped to the current milestone.

Do not run terraform apply unless explicitly requested.

The user runs every `terraform apply`/`destroy` and every `kubectl`/`helm` change themselves. The agent only reads, runs `fmt`/`validate`/`plan`, and gives exact commands. `.claude/settings.json` denies mutating commands; do not edit or weaken it.

Do not use `-target` as a normal workflow. It is only a stop-gap while the flat root is being split (see "Terraform layout").

Use:
- terraform fmt
- terraform validate
- terraform plan

Tag supported resources with:
- Project = enterprise-ai-platform
- Environment = dev
- ManagedBy = terraform


### Terraform layout

Keep Terraform simple and production-shaped.

**Decision (see docs/decisions/terraform-stacks.md): split into three stacks, one state each, plain `terraform apply`, no `-target`.** Do not collapse these back into one root, and do not move files between stacks without asking.

terraform/environments/dev/
├── app/          Cognito, API Gateway, gateway Lambda, RAG, AgentCore, approval, Lambda security group, CloudWatch dashboard
├── eks/          EKS cluster, system node group, add-ons, cluster and node IAM
└── eks-addons/   Karpenter, GPU NodePool, NVIDIA device plugin, Load Balancer Controller, inference NLB security group, CloudWatch Observability add-on, manifests/

Each stack has its own versions.tf, providers.tf, variables.tf, locals.tf, outputs.tf, backend.tf (local state) and terraform.tfvars.example. Order: app, eks, eks-addons, kubectl manifests (app exports the Lambda security group; the Lambda finds the NLB hostname itself at runtime, so app runs once). Teardown: delete the Service, Deployment and NodePool, then eks-addons, eks, app (never app before eks-addons).

**Migration status: done (2026-11-09). Files are moved, each stack passes `init`/`validate`/`plan`. The user is doing the first full creation run (app and eks applied; eks-addons and the vLLM Service followed).** `eks-addons` reads the Lambda security group from `../app/terraform.tfstate` (output `lambda_security_group_id`) and the cluster through `data "aws_eks_cluster"`, so it fails to plan until `app` and `eks` are applied: that is the ordering guard. Leftovers in the flat `dev/` (`.build`, `.terraform`, `asd.json`, `tf.plan`, `terraform.tfstate*`) are not part of any stack; ask before deleting.

Within each stack, split resources by concern (iam.tf, lambda.tf, eks.tf and so on).

Do not put all resources into one large main.tf.

Do not create reusable Terraform modules yet.
Only introduce modules when there is real repetition or reuse.

Pin:
- Terraform required_version
- AWS provider version
- Lambda runtime
- Bedrock model ID where applicable

Keep Terraform readable and minimal.
Do not spend time building abstractions that do not directly help the current platform milestone.



## Repository rules

Current structure:

enterprise-ai-platform/
├── README.md
├── CLAUDE.md
├── data/
│   └── *.md                  (RAG source documents, uploaded to S3 by Terraform)
├── docs/
│   ├── progress.md
│   ├── decisions/
│   ├── learning/
│   ├── terraform/
│   └── troubleshooting/
├── terraform/
│   └── environments/
│       └── dev/
├── gateway/
│   ├── lambda/               (handler.py, agent.py, model.py, rag.py, mcp_client.py, approval.py, vllm.py)
│   └── tests/                (pytest; fake OpenAI-compatible server, no GPU or AWS needed)
├── write-executor/
├── scripts/                  (approve.py)
├── agent/
│   └── agent.py
├── mcp-server/
│   └── server.py
├── mcp-agentcore/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py
├── EnterpriseAIMCP/          (AgentCore project, managed with its own CDK)
└── experiments/

Do not create large folder structures prematurely.

## Learning requirement

I need to understand the project.

For every meaningful change, explain:
- why it exists
- how it works
- what depends on it
- how it can fail
- what I should verify

Do not make large opaque changes.