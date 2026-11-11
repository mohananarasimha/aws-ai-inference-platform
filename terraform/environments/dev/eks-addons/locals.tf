locals {
  project     = "enterprise-ai-platform"
  environment = "dev"
  name        = "${local.project}-${local.environment}"

  tags = {
    Project     = local.project
    Environment = local.environment
    ManagedBy   = "terraform"
  }
}
