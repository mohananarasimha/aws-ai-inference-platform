data "aws_caller_identity" "current" {}

# The cluster is created by the eks stack; apply that first.
data "aws_eks_cluster" "main" {
  name = var.eks_cluster_name
}
