resource "aws_ec2_tag" "karpenter_subnet_discovery" {
  for_each = toset(data.aws_eks_cluster.main.vpc_config[0].subnet_ids)

  resource_id = each.value
  key         = "karpenter.sh/discovery"
  value       = var.eks_cluster_name
}

resource "aws_ec2_tag" "karpenter_cluster_sg_discovery" {
  resource_id = data.aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  key         = "karpenter.sh/discovery"
  value       = var.eks_cluster_name
}

resource "aws_eks_access_entry" "karpenter_node" {
  cluster_name  = data.aws_eks_cluster.main.name
  principal_arn = aws_iam_role.karpenter_node.arn
  type          = "EC2_LINUX"
}

resource "aws_eks_pod_identity_association" "karpenter" {
  cluster_name    = data.aws_eks_cluster.main.name
  namespace       = "kube-system"
  service_account = "karpenter"
  role_arn        = aws_iam_role.karpenter_controller.arn
}

resource "helm_release" "karpenter" {
  name       = "karpenter"
  namespace  = "kube-system"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.karpenter_version
  wait       = true
  timeout    = 600

  values = [yamlencode({
    replicas = 1

    settings = {
      clusterName     = data.aws_eks_cluster.main.name
      eksControlPlane = true
    }

    controller = {
      resources = {
        requests = {
          cpu    = "500m"
          memory = "512Mi"
        }
        limits = {
          memory = "1Gi"
        }
      }
    }
  })]

  depends_on = [
    aws_eks_pod_identity_association.karpenter,
    aws_eks_access_entry.karpenter_node,
    aws_iam_role_policy_attachment.karpenter_controller,
    aws_iam_role_policy_attachment.karpenter_node,
    aws_ec2_tag.karpenter_subnet_discovery,
    aws_ec2_tag.karpenter_cluster_sg_discovery,
  ]
}
