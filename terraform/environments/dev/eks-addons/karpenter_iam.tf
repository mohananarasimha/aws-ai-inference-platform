data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

locals {
  eks_node_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ]
}

resource "aws_iam_role" "karpenter_node" {
  name               = "${local.name}-karpenter-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "karpenter_node" {
  for_each = toset(local.eks_node_policy_arns)

  role       = aws_iam_role.karpenter_node.name
  policy_arn = each.value
}

data "aws_iam_policy_document" "karpenter_controller_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "karpenter_controller" {
  name               = "${local.name}-karpenter-controller"
  assume_role_policy = data.aws_iam_policy_document.karpenter_controller_assume.json
}

locals {
  karpenter_account = data.aws_caller_identity.current.account_id
  karpenter_ec2     = "arn:aws:ec2:${var.aws_region}"
  karpenter_owned   = "kubernetes.io/cluster/${var.eks_cluster_name}"

  karpenter_launch_resources = [
    "${local.karpenter_ec2}:*:fleet/*",
    "${local.karpenter_ec2}:*:instance/*",
    "${local.karpenter_ec2}:*:volume/*",
    "${local.karpenter_ec2}:*:network-interface/*",
    "${local.karpenter_ec2}:*:launch-template/*",
    "${local.karpenter_ec2}:*:spot-instances-request/*",
  ]
}

data "aws_iam_policy_document" "karpenter_node_lifecycle" {
  statement {
    sid     = "AllowScopedEC2InstanceAccessActions"
    actions = ["ec2:RunInstances", "ec2:CreateFleet"]
    resources = [
      "${local.karpenter_ec2}::image/*",
      "${local.karpenter_ec2}::snapshot/*",
      "${local.karpenter_ec2}:*:security-group/*",
      "${local.karpenter_ec2}:*:subnet/*",
      "${local.karpenter_ec2}:*:capacity-reservation/*",
      "${local.karpenter_ec2}:*:placement-group/*",
    ]
  }

  statement {
    sid       = "AllowScopedEC2LaunchTemplateAccessActions"
    actions   = ["ec2:RunInstances", "ec2:CreateFleet"]
    resources = ["${local.karpenter_ec2}:*:launch-template/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid       = "AllowScopedEC2InstanceActionsWithTags"
    actions   = ["ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateLaunchTemplate"]
    resources = local.karpenter_launch_resources

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.eks_cluster_name]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid       = "AllowScopedResourceCreationTagging"
    actions   = ["ec2:CreateTags"]
    resources = local.karpenter_launch_resources

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.eks_cluster_name]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateFleet", "CreateLaunchTemplate"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid       = "AllowScopedResourceTagging"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.karpenter_ec2}:*:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }

    condition {
      test     = "StringEqualsIfExists"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.eks_cluster_name]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["eks:eks-cluster-name", "karpenter.sh/nodeclaim", "Name"]
    }
  }

  statement {
    sid     = "AllowScopedDeletion"
    actions = ["ec2:TerminateInstances", "ec2:RebootInstances", "ec2:DeleteLaunchTemplate"]
    resources = [
      "${local.karpenter_ec2}:*:instance/*",
      "${local.karpenter_ec2}:*:launch-template/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }
}

data "aws_iam_policy_document" "karpenter_iam_integration" {
  statement {
    sid       = "AllowPassingInstanceRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.karpenter_node.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com", "ec2.amazonaws.com.cn"]
    }
  }

  statement {
    sid       = "AllowScopedInstanceProfileCreationActions"
    actions   = ["iam:CreateInstanceProfile"]
    resources = ["arn:aws:iam::${local.karpenter_account}:instance-profile/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.eks_cluster_name]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/topology.kubernetes.io/region"
      values   = [var.aws_region]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.k8s.aws/ec2nodeclass"
      values   = ["*"]
    }
  }

  statement {
    sid       = "AllowScopedInstanceProfileTagActions"
    actions   = ["iam:TagInstanceProfile"]
    resources = ["arn:aws:iam::${local.karpenter_account}:instance-profile/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/topology.kubernetes.io/region"
      values   = [var.aws_region]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.eks_cluster_name]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/topology.kubernetes.io/region"
      values   = [var.aws_region]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass"
      values   = ["*"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.k8s.aws/ec2nodeclass"
      values   = ["*"]
    }
  }

  statement {
    sid = "AllowScopedInstanceProfileActions"
    actions = [
      "iam:AddRoleToInstanceProfile",
      "iam:RemoveRoleFromInstanceProfile",
      "iam:DeleteInstanceProfile",
    ]
    resources = ["arn:aws:iam::${local.karpenter_account}:instance-profile/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/${local.karpenter_owned}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/topology.kubernetes.io/region"
      values   = [var.aws_region]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass"
      values   = ["*"]
    }
  }
}

data "aws_iam_policy_document" "karpenter_eks_integration" {
  statement {
    sid       = "AllowAPIServerEndpointDiscovery"
    actions   = ["eks:DescribeCluster"]
    resources = [data.aws_eks_cluster.main.arn]
  }
}

data "aws_iam_policy_document" "karpenter_resource_discovery" {
  statement {
    sid = "AllowRegionalReadActions"
    actions = [
      "ec2:DescribeCapacityReservations",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribePlacementGroups",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeSubnets",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid       = "AllowSSMReadActions"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}::parameter/aws/service/*"]
  }

  statement {
    sid       = "AllowPricingReadActions"
    actions   = ["pricing:GetProducts"]
    resources = ["*"]
  }

  statement {
    sid       = "AllowUnscopedInstanceProfileListAction"
    actions   = ["iam:ListInstanceProfiles"]
    resources = ["*"]
  }

  statement {
    sid       = "AllowInstanceProfileReadActions"
    actions   = ["iam:GetInstanceProfile"]
    resources = ["arn:aws:iam::${local.karpenter_account}:instance-profile/*"]
  }
}

locals {
  karpenter_controller_policies = {
    node-lifecycle     = data.aws_iam_policy_document.karpenter_node_lifecycle.json
    iam-integration    = data.aws_iam_policy_document.karpenter_iam_integration.json
    eks-integration    = data.aws_iam_policy_document.karpenter_eks_integration.json
    resource-discovery = data.aws_iam_policy_document.karpenter_resource_discovery.json
  }
}

resource "aws_iam_policy" "karpenter_controller" {
  for_each = local.karpenter_controller_policies

  name   = "${local.name}-karpenter-${each.key}"
  policy = each.value
}

resource "aws_iam_role_policy_attachment" "karpenter_controller" {
  for_each = aws_iam_policy.karpenter_controller

  role       = aws_iam_role.karpenter_controller.name
  policy_arn = each.value.arn
}
