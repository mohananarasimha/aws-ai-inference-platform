data "aws_iam_policy_document" "aws_lbc_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "aws_lbc" {
  name               = "${local.name}-aws-lbc"
  assume_role_policy = data.aws_iam_policy_document.aws_lbc_assume.json
}

# Official policy for the pinned controller version, unmodified.
resource "aws_iam_policy" "aws_lbc" {
  name   = "${local.name}-aws-lbc"
  policy = file("${path.module}/policies/aws-load-balancer-controller-v${var.aws_lbc_version}.json")
}

resource "aws_iam_role_policy_attachment" "aws_lbc" {
  role       = aws_iam_role.aws_lbc.name
  policy_arn = aws_iam_policy.aws_lbc.arn
}

resource "aws_eks_pod_identity_association" "aws_lbc" {
  cluster_name    = data.aws_eks_cluster.main.name
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = aws_iam_role.aws_lbc.arn
}

resource "helm_release" "aws_lbc" {
  name       = "aws-load-balancer-controller"
  namespace  = "kube-system"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.aws_lbc_version
  wait       = true
  timeout    = 600

  values = [yamlencode({
    clusterName  = data.aws_eks_cluster.main.name
    region       = var.aws_region
    vpcId        = var.vpc_id
    replicaCount = 1

    # Services set loadBalancerClass explicitly; the webhook would only block
    # Service creation (e.g. Karpenter's) while the controller is starting.
    enableServiceMutatorWebhook = false

    serviceAccount = {
      create = true
      name   = "aws-load-balancer-controller"
    }

    nodeSelector = {
      role = "system"
    }

    resources = {
      requests = {
        cpu    = "100m"
        memory = "128Mi"
      }
      limits = {
        memory = "256Mi"
      }
    }
  })]

  depends_on = [
    aws_eks_pod_identity_association.aws_lbc,
    aws_iam_role_policy_attachment.aws_lbc,
  ]
}
