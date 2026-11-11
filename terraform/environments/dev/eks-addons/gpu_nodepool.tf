resource "helm_release" "karpenter_gpu_nodepool" {
  name      = "karpenter-gpu"
  namespace = "kube-system"
  chart     = "${path.module}/charts/karpenter-gpu"
  wait      = true
  timeout   = 300

  values = [yamlencode({
    clusterName    = data.aws_eks_cluster.main.name
    nodeRole       = aws_iam_role.karpenter_node.name
    instanceType   = var.gpu_instance_type
    rootVolumeSize = "100Gi"
    maxCpu         = 4
    maxGpus        = 1
    tags           = local.tags
  })]

  depends_on = [helm_release.karpenter]
}
