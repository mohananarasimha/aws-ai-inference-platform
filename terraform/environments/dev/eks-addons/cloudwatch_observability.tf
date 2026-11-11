data "aws_iam_policy_document" "cloudwatch_agent_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cloudwatch_agent" {
  name               = "${local.name}-cloudwatch-agent"
  assume_role_policy = data.aws_iam_policy_document.cloudwatch_agent_assume.json
}

# The policy AWS recommends for this add-on. Narrowing it is deferred.
resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.cloudwatch_agent.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# OTel Container Insights only: kubelet, cAdvisor, DCGM (GPU), kube-state-metrics
# and the vLLM /metrics endpoint go to CloudWatch over OTLP. Logs, Application
# Signals and the legacy Container Insights pipeline are off.
resource "aws_eks_addon" "cloudwatch_observability" {
  cluster_name                = data.aws_eks_cluster.main.name
  addon_name                  = "amazon-cloudwatch-observability"
  addon_version               = var.cloudwatch_observability_version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  configuration_values = jsonencode({
    containerInsights  = { enabled = false }
    applicationSignals = { enabled = false }
    containerLogs      = { enabled = false }

    otelContainerInsights = {
      enabled = true
      logs    = { enabled = false }

      solutions = {
        karpenter = { enabled = false }
        keda      = { enabled = false }
        kserve    = { controlPlane = { enabled = false } }
        knative   = { dataPlane = { enabled = false }, controlPlane = { enabled = false } }
        vllm      = { enabled = true }
      }
    }

    nodeExporter  = { enabled = false }
    neuronMonitor = { enabled = false }
  })

  pod_identity_association {
    role_arn        = aws_iam_role.cloudwatch_agent.arn
    service_account = "cloudwatch-agent"
  }

  depends_on = [aws_iam_role_policy_attachment.cloudwatch_agent]
}
