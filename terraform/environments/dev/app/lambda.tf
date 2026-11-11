locals {
  lambda_src_dir   = "${path.module}/../../../../gateway/lambda"
  lambda_build_dir = "${path.module}/.build/lambda"
}

resource "terraform_data" "lambda_build" {
  triggers_replace = {
    source = sha1(join("", [
      for f in sort(fileset(local.lambda_src_dir, "*.py")) : filemd5("${local.lambda_src_dir}/${f}")
    ]))
    requirements = filemd5("${local.lambda_src_dir}/requirements.txt")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf "${local.lambda_build_dir}"
      mkdir -p "${local.lambda_build_dir}"
      python3 -m pip install \
        --requirement "${local.lambda_src_dir}/requirements.txt" \
        --target "${local.lambda_build_dir}" \
        --platform manylinux2014_x86_64 \
        --python-version 3.12 \
        --implementation cp \
        --only-binary=:all: \
        --no-compile \
        --upgrade
      cp "${local.lambda_src_dir}"/*.py "${local.lambda_build_dir}/"
    EOT
  }
}

data "archive_file" "gateway_lambda" {
  type        = "zip"
  source_dir  = local.lambda_build_dir
  output_path = "${path.module}/.build/gateway-lambda.zip"

  depends_on = [terraform_data.lambda_build]
}

resource "aws_cloudwatch_log_group" "gateway_lambda" {
  name              = "/aws/lambda/${local.name}-gateway"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "gateway" {
  function_name = local.gateway_function_name
  role          = aws_iam_role.gateway_lambda.arn
  runtime       = var.lambda_runtime
  handler       = "handler.lambda_handler"
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_size

  filename         = data.archive_file.gateway_lambda.output_path
  source_code_hash = data.archive_file.gateway_lambda.output_base64sha256

  environment {
    variables = {
      KNOWLEDGE_BASE_ID          = aws_bedrockagent_knowledge_base.rag.id,
      AGENTCORE_RUNTIME_ARN      = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
      APPROVAL_STATE_MACHINE_ARN = aws_sfn_state_machine.approval.arn
      VLLM_PORT                  = tostring(var.inference_port)
    }
  }

  vpc_config {
    subnet_ids         = var.lambda_subnet_ids
    security_group_ids = [aws_security_group.gateway_lambda.id]
  }

  depends_on = [
    aws_cloudwatch_log_group.gateway_lambda,
    aws_iam_role_policy_attachment.gateway_lambda_logs,
    aws_iam_role_policy_attachment.gateway_lambda_vpc,
    aws_vpc_security_group_ingress_rule.router_from_lambda,
    aws_iam_role_policy.bedrock_invoke,
    aws_iam_role_policy.knowledge_base_retrieve,
    aws_iam_role_policy.gateway_start_approval,
    aws_iam_role_policy.vllm_discovery,
    aws_vpc_security_group_egress_rule.gateway_lambda_https,
  ]
}
