output "eks_cluster_name" {
  description = "EKS cluster name"
  value       = aws_eks_cluster.main.name
}

output "eks_cluster_endpoint" {
  description = "EKS API server endpoint"
  value       = aws_eks_cluster.main.endpoint
}

output "eks_update_kubeconfig_command" {
  description = "Command that adds this cluster to your kubeconfig"
  value       = "aws eks update-kubeconfig --name ${aws_eks_cluster.main.name} --region ${var.aws_region}"
}
