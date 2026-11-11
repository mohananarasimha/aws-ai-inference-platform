resource "aws_apigatewayv2_api" "gateway" {
  name          = "${local.name}-gateway"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "gateway" {
  api_id                 = aws_apigatewayv2_api.gateway.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.gateway.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "invoke" {
  api_id    = aws_apigatewayv2_api.gateway.id
  route_key = "POST /invoke"
  target    = "integrations/${aws_apigatewayv2_integration.gateway.id}"

  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.jwt.id
  authorization_scopes = ["${aws_cognito_resource_server.gateway.identifier}/invoke"]
}

# Throttled because each call costs Bedrock tokens.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.gateway.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
}

resource "aws_lambda_permission" "apigateway_invoke" {
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.gateway.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.gateway.execution_arn}/*/POST/invoke"
}
