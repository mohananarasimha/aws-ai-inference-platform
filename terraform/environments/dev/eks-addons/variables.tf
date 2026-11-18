variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "allowed_account_ids" {
  description = "AWS account IDs Terraform may run against (the personal profile account)"
  type        = list(string)
}

variable "vpc_id" {
  description = "VPC that contains the WireGuard/NAT router and the Lambda subnet (the default VPC)"
  type        = string
}

variable "eks_cluster_name" {
  description = "EKS cluster name (also the Karpenter discovery tag value)"
  type        = string
  default     = "ai-inference-lab"
}

variable "gpu_instance_type" {
  description = "Only GPU instance type Karpenter may launch (account quota is 4 vCPUs)"
  type        = string
  default     = "g5.xlarge"
}

variable "karpenter_version" {
  description = "Karpenter Helm chart version"
  type        = string
  default     = "1.15.1"
}

variable "nvidia_device_plugin_version" {
  description = "NVIDIA k8s-device-plugin Helm chart version"
  type        = string
  default     = "0.20.1"
}

variable "aws_lbc_version" {
  description = "AWS Load Balancer Controller Helm chart and app version; must match policies/aws-load-balancer-controller-v<version>.json"
  type        = string
  default     = "3.6.2"
}

variable "cloudwatch_observability_version" {
  description = "amazon-cloudwatch-observability EKS add-on version (v7.0.1 supports Kubernetes 1.29 to 1.37)"
  type        = string
  default     = "v7.0.1-eksbuild.1"
}

variable "inference_port" {
  description = "Port of the in-cluster vLLM Service behind the internal NLB"
  type        = number
  default     = 8000
}
