output "karpenter_node_role_name" {
  description = "IAM role for nodes launched by Karpenter"
  value       = aws_iam_role.karpenter_node.name
}
