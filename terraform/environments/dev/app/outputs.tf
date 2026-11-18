output "lambda_function_name" {
  description = "Gateway Lambda function name"
  value       = aws_lambda_function.gateway.function_name
}

output "lambda_function_arn" {
  description = "Gateway Lambda function ARN"
  value       = aws_lambda_function.gateway.arn
}

output "lambda_role_arn" {
  description = "Gateway Lambda execution role ARN"
  value       = aws_iam_role.gateway_lambda.arn
}

output "invoke_url" {
  description = "Full URL for POST /invoke"
  value       = "${aws_apigatewayv2_api.gateway.api_endpoint}/invoke"
}

output "cognito_token_endpoint" {
  description = "OAuth2 token endpoint for client credentials"
  value       = "https://${aws_cognito_user_pool_domain.gateway.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
}

output "cognito_openid_configuration_url" {
  description = "OpenID Connect discovery document of the Cognito user pool (issuer and JWKS used by the API authorizer)"
  value       = "https://cognito-idp.us-east-1.amazonaws.com/${aws_cognito_user_pool.gateway.id}/.well-known/openid-configuration"
}

output "m2m_client_id" {
  description = "M2M app client ID"
  value       = aws_cognito_user_pool_client.m2m.id
}

output "m2m_client_secret" {
  description = "M2M app client secret"
  value       = aws_cognito_user_pool_client.m2m.client_secret
  sensitive   = true
}

output "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID (set as KNOWLEDGE_BASE_ID on the Lambda)"
  value       = aws_bedrockagent_knowledge_base.rag.id
}

output "knowledge_base_data_source_id" {
  description = "Data source ID, needed to start ingestion jobs"
  value       = aws_bedrockagent_data_source.rag_source.data_source_id
}

output "rag_source_bucket_name" {
  description = "S3 bucket holding RAG source documents"
  value       = aws_s3_bucket.rag_source.id
}

output "rag_vector_bucket_name" {
  description = "S3 Vectors bucket name"
  value       = aws_s3vectors_vector_bucket.rag.vector_bucket_name
}

output "rag_vector_index_arn" {
  description = "S3 Vectors index ARN"
  value       = aws_s3vectors_index.rag.index_arn
}

output "agentcore_runtime_arn" {
  description = "AgentCore Runtime ARN"
  value       = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
}

output "agentcore_mcp_url" {
  description = "AgentCore MCP invocation URL (for MCP Inspector)"
  value       = "https://bedrock-agentcore.${var.aws_region}.amazonaws.com/runtimes/${urlencode(aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn)}/invocations?qualifier=DEFAULT"
}

output "approval_queue_url" {
  description = "SQS queue that receives approval requests"
  value       = aws_sqs_queue.approval.url
}

output "approval_state_machine_arn" {
  description = "Step Functions approval workflow ARN"
  value       = aws_sfn_state_machine.approval.arn
}

output "write_executor_function_name" {
  description = "Privileged Write Executor Lambda"
  value       = aws_lambda_function.write_executor.function_name
}

output "lambda_security_group_id" {
  description = "Gateway Lambda security group; read by eks-addons through remote state"
  value       = aws_security_group.gateway_lambda.id
}
