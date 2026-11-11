data "archive_file" "write_executor" {
  type        = "zip"
  source_dir  = "${path.module}/../../../../write-executor"
  output_path = "${path.module}/.build/write-executor.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

resource "aws_iam_role" "write_executor" {
  name               = "${local.name}-write-executor"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "write_executor_logs" {
  role       = aws_iam_role.write_executor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "write_executor" {
  statement {
    sid = "UpdateGatewayLambdaConfiguration"
    actions = [
      "lambda:UpdateFunctionConfiguration",
      "lambda:GetFunctionConfiguration",
    ]
    resources = ["arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.gateway_function_name}"]
  }
}

resource "aws_iam_role_policy" "write_executor" {
  name   = "write_executor_permissions"
  role   = aws_iam_role.write_executor.id
  policy = data.aws_iam_policy_document.write_executor.json
}

resource "aws_cloudwatch_log_group" "write_executor" {
  name              = "/aws/lambda/${local.name}-write-executor"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "write_executor" {
  function_name = "${local.name}-write-executor"
  role          = aws_iam_role.write_executor.arn
  runtime       = var.lambda_runtime
  handler       = "handler.lambda_handler"
  timeout       = 90
  memory_size   = 128

  filename         = data.archive_file.write_executor.output_path
  source_code_hash = data.archive_file.write_executor.output_base64sha256

  environment {
    variables = {
      TARGET_FUNCTION_NAME = local.gateway_function_name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.write_executor,
    aws_iam_role_policy_attachment.write_executor_logs,
  ]
}
