locals {
  project     = "enterprise-ai-platform"
  environment = "dev"
  name        = "${local.project}-${local.environment}"

  gateway_function_name = "${local.project}-${local.environment}-gateway"

  tags = {
    Project     = local.project
    Environment = local.environment
    ManagedBy   = "terraform"
  }
}
