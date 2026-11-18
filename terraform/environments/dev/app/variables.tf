variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "allowed_account_ids" {
  description = "AWS account IDs Terraform may run against (the personal profile account)"
  type        = list(string)
}

variable "bedrock_model_id_1" {
  description = "Bedrock model the gateway Lambda is allowed to invoke (must match MODEL_ID in handler.py)"
  type        = string
  default     = "amazon.nova-micro-v1:0"
}

variable "bedrock_model_id_2" {
  description = "Bedrock model the gateway Lambda is allowed to invoke (must match MODEL_ID in handler.py)"
  type        = string
  default     = "amazon.nova-lite-v1:0"
}

variable "lambda_runtime" {
  description = "Lambda runtime"
  type        = string
  default     = "python3.12"
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Lambda memory in MB"
  type        = number
  default     = 256
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 14
}

variable "embedding_model_id" {
  description = "Bedrock embedding model used by the Knowledge Base"
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "vpc_id" {
  description = "VPC that contains the WireGuard/NAT router and the Lambda subnet (the default VPC)"
  type        = string
}

variable "lambda_subnet_ids" {
  description = "Dedicated private subnet(s) for the gateway Lambda, in the same AZ as the router"
  type        = list(string)
}

variable "wireguard_eni_id" {
  description = "ENI of the manually created WireGuard/NAT EC2 router (route target)"
  type        = string
}

variable "inference_port" {
  description = "Port of the in-cluster vLLM Service behind the internal NLB; the Lambda appends it to the discovered NLB name"
  type        = number
  default     = 8000
}

variable "onprem_vllm_base_url" {
  description = "Base URL of the on-prem vLLM endpoint (the Nginx load balancer in front of the two replicas), reached through the WireGuard router. Used when a request says provider=vllm and target=onprem."
  type        = string
  default     = "http://10.100.0.3:18082"

  validation {
    condition     = can(regex("^http://[0-9.]+:[0-9]+$", var.onprem_vllm_base_url))
    error_message = "onprem_vllm_base_url must look like http://<ipv4>:<port>; the security group rules are derived from it."
  }
}

variable "api_throttling_rate_limit" {
  description = "Steady-state requests per second allowed on the API Gateway $default stage (every call can cost Bedrock tokens or GPU time)"
  type        = number
  default     = 5
}

variable "api_throttling_burst_limit" {
  description = "Requests the API Gateway $default stage accepts in a burst above the rate limit"
  type        = number
  default     = 10
}
