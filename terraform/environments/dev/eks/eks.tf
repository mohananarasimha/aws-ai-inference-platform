data "aws_ec2_instance_type_offerings" "gpu" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.gpu_instance_type]
  }
}

# Existing default-VPC subnets only, limited to AZs that offer the GPU type.
# Nodes get outbound access through the existing internet gateway.
data "aws_subnets" "eks" {
  filter {
    name   = "vpc-id"
    values = [var.vpc_id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }

  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.gpu.locations
  }
}

resource "aws_eks_cluster" "main" {
  name     = var.eks_cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.eks_kubernetes_version

  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    subnet_ids              = data.aws_subnets.eks.ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.eks_public_access_cidrs
  }

  upgrade_policy {
    support_type = "STANDARD"
  }

  lifecycle {
    precondition {
      condition     = length(data.aws_subnets.eks.ids) >= 2
      error_message = "EKS needs existing subnets in at least two AZs."
    }
  }

  depends_on = [aws_iam_role_policy_attachment.eks_cluster]
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "eks-pod-identity-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.eks_system_node.arn
  subnet_ids      = data.aws_subnets.eks.ids
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  instance_types  = ["t3.medium"]

  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 1
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    role = "system"
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_system_node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
    aws_eks_addon.pod_identity_agent,
  ]
}

resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.system]
}
