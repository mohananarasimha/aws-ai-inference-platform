data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gateway_lambda" {
  name               = "${local.name}-gateway-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "gateway_lambda_logs" {
  role       = aws_iam_role.gateway_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The Converse API is authorized by bedrock:InvokeModel.
data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid       = "InvokeConfiguredModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id_1}", "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id_2}"]
  }
}

resource "aws_iam_role_policy" "bedrock_invoke" {
  name   = "bedrock_invoke_permissions"
  role   = aws_iam_role.gateway_lambda.id
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}

data "aws_iam_policy_document" "knowledge_base_retrieve" {
  statement {
    sid       = "RetrieveFromKnowledgeBase"
    effect    = "Allow"
    actions   = ["bedrock:Retrieve"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${aws_bedrockagent_knowledge_base.rag.id}"]
  }
}

resource "aws_iam_role_policy" "knowledge_base_retrieve" {
  name   = "knowledge_base_retrieve_permissions"
  role   = aws_iam_role.gateway_lambda.id
  policy = data.aws_iam_policy_document.knowledge_base_retrieve.json
}

# Neither action supports resource-level permissions. The Lambda uses them to
# find the NLB the load balancer controller built for the vllm Service.
data "aws_iam_policy_document" "vllm_discovery" {
  statement {
    sid       = "FindInferenceLoadBalancer"
    actions   = ["tag:GetResources", "elasticloadbalancing:DescribeLoadBalancers"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "vllm_discovery" {
  name   = "vllm_discovery_permissions"
  role   = aws_iam_role.gateway_lambda.id
  policy = data.aws_iam_policy_document.vllm_discovery.json
}

data "aws_iam_policy_document" "gateway_start_approval" {
  statement {
    sid       = "StartApprovalWorkflow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.approval.arn]
  }
}

resource "aws_iam_role_policy" "gateway_start_approval" {
  name   = "gateway_start_approval_permissions"
  role   = aws_iam_role.gateway_lambda.id
  policy = data.aws_iam_policy_document.gateway_start_approval.json
}

resource "aws_iam_role_policy_attachment" "gateway_lambda_vpc" {
  role       = aws_iam_role.gateway_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}
