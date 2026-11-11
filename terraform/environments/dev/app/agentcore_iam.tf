data "aws_iam_policy_document" "agentcore_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "agentcore_runtime" {
  name               = "${local.name}-agentcore-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
}

data "aws_iam_policy_document" "agentcore_runtime_base" {
  statement {
    sid       = "ReadCodeArtifact"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.agentcore_artifacts.arn}/*"]
  }

  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid = "WriteRuntimeLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
}

resource "aws_iam_role_policy" "agentcore_runtime_base" {
  name   = "agentcore_runtime_base"
  role   = aws_iam_role.agentcore_runtime.id
  policy = data.aws_iam_policy_document.agentcore_runtime_base.json
}

data "aws_iam_policy_document" "agentcore_mcp_tools" {
  statement {
    sid       = "ReadGatewayLambdaConfiguration"
    actions   = ["lambda:GetFunctionConfiguration"]
    resources = [aws_lambda_function.gateway.arn]
  }

  statement {
    sid       = "ReadGatewayLambdaLogs"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.gateway_lambda.arn}:*"]
  }
}

resource "aws_iam_role_policy" "agentcore_mcp_tools" {
  name   = "agentcore_mcp_tools"
  role   = aws_iam_role.agentcore_runtime.id
  policy = data.aws_iam_policy_document.agentcore_mcp_tools.json
}
