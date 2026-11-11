import os

REGION = "us-east-1"
MAX_AGENT_STEPS = 5


MODEL_MAP = {
    "fast": "amazon.nova-micro-v1:0",
    "quality": "amazon.nova-lite-v1:0"
}


VLLM_MODEL = "Qwen/Qwen2.5-7B-Instruct"
VLLM_MAX_TOKENS = 512

METRICS_NAMESPACE = "EnterpriseAIPlatform/Gateway"

# ---- End-to-end request budget -------------------------------------------------
# The Lambda timeout is 30 s and the HTTP API integration limit is 30 s too
# (no timeout_milliseconds is set in Terraform). The request budget stays 6 s
# below that: time to unwind the MCP session, log and return through API
# Gateway. It can be lowered with the REQUEST_TIMEOUT_SECONDS env var.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", 24))
LAMBDA_RESERVE_SECONDS = 6

# Below this much remaining budget no new model/tool/RAG call is started.
MIN_CALL_SECONDS = 1

# API Gateway starts its clock before Lambda does (cold start); the time it
# reports between the two is subtracted from the budget, capped here.
MAX_QUEUED_SECONDS = 10

# ---- Per-component maximums: a call gets min(maximum, remaining budget) ---------
BEDROCK_MAX_TIMEOUT_SECONDS = 20
VLLM_MAX_TIMEOUT_SECONDS = 20
RAG_MAX_TIMEOUT_SECONDS = 8
APPROVAL_MAX_TIMEOUT_SECONDS = 5
DISCOVERY_MAX_TIMEOUT_SECONDS = 5
MCP_MAX_TIMEOUT_SECONDS = 15
MCP_CONNECT_TIMEOUT_SECONDS = 5
# Upper bound for the MCP session's HTTP read wait when no deadline is given.
MCP_SESSION_MAX_TIMEOUT_SECONDS = 120
AWS_CONNECT_TIMEOUT_SECONDS = 3

# The load balancer controller tags the NLB it creates for the vllm Service.
VLLM_NLB_TAG_KEY = "service.k8s.aws/stack"
VLLM_NLB_TAG_VALUE = "default/vllm"
