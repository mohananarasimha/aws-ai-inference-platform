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

variable "eks_kubernetes_version" {
  description = "EKS Kubernetes version; must be supported by var.karpenter_version"
  type        = string
  default     = "1.36"
}

variable "eks_public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint (IAM auth still applies)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "gpu_instance_type" {
  description = "Only GPU instance type Karpenter may launch (account quota is 4 vCPUs)"
  type        = string
  default     = "g5.xlarge"
}
