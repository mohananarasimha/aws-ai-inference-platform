data "aws_caller_identity" "current" {}

resource "aws_cognito_user_pool" "gateway" {
  name = "${local.name}-gateway"
}

resource "aws_cognito_resource_server" "gateway" {
  user_pool_id = aws_cognito_user_pool.gateway.id
  identifier   = "enterprise-ai-gateway"
  name         = "${local.name}-gateway"

  scope {
    scope_name        = "invoke"
    scope_description = "Call POST /invoke"
  }
}

resource "aws_cognito_user_pool_domain" "gateway" {
  domain       = "${local.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.gateway.id
}

resource "aws_cognito_user_pool_client" "m2m" {
  name         = "${local.name}-m2m"
  user_pool_id = aws_cognito_user_pool.gateway.id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = ["${aws_cognito_resource_server.gateway.identifier}/invoke"]
}

# Access tokens from client_credentials carry client_id instead of aud; API Gateway accepts either.
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.gateway.id
  name             = "${local.name}-jwt"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.gateway.id}"
    audience = [aws_cognito_user_pool_client.m2m.id]
  }
}
