locals {
  mcp_src_dir   = "${path.module}/../../../../mcp-agentcore"
  mcp_build_dir = "${path.module}/.build/agentcore"

  agentcore_runtime_name = "${replace(local.name, "-", "_")}_mcp"

  mcp_source_hash = sha1(join("", [
    filemd5("${local.mcp_src_dir}/server.py"),
    filemd5("${local.mcp_src_dir}/requirements.txt"),
  ]))
}

resource "aws_s3_bucket" "agentcore_artifacts" {
  bucket = "${local.name}-agentcore-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "agentcore_artifacts" {
  bucket = aws_s3_bucket.agentcore_artifacts.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agentcore_artifacts" {
  bucket = aws_s3_bucket.agentcore_artifacts.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "terraform_data" "agentcore_build" {
  triggers_replace = local.mcp_source_hash

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf "${local.mcp_build_dir}"
      mkdir -p "${local.mcp_build_dir}"
      python3 -m pip install \
        --requirement "${local.mcp_src_dir}/requirements.txt" \
        --target "${local.mcp_build_dir}" \
        --platform manylinux2014_aarch64 \
        --python-version 3.14 \
        --implementation cp \
        --only-binary=:all: \
        --no-compile \
        --upgrade
      cp "${local.mcp_src_dir}/server.py" "${local.mcp_build_dir}/main.py"
    EOT
  }
}

data "archive_file" "agentcore_mcp" {
  type        = "zip"
  source_dir  = local.mcp_build_dir
  output_path = "${path.module}/.build/agentcore-mcp.zip"

  depends_on = [terraform_data.agentcore_build]
}

resource "aws_s3_object" "agentcore_mcp" {
  bucket      = aws_s3_bucket.agentcore_artifacts.id
  key         = "mcp/${local.mcp_source_hash}.zip"
  source      = data.archive_file.agentcore_mcp.output_path
  source_hash = data.archive_file.agentcore_mcp.output_md5
}

resource "aws_bedrockagentcore_agent_runtime" "mcp" {
  agent_runtime_name = local.agentcore_runtime_name
  description        = "Enterprise AI platform MCP server"
  role_arn           = aws_iam_role.agentcore_runtime.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["main.py"]
      runtime     = "PYTHON_3_14"

      code {
        s3 {
          bucket = aws_s3_bucket.agentcore_artifacts.id
          prefix = aws_s3_object.agentcore_mcp.key
        }
      }
    }
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.gateway.id}/.well-known/openid-configuration"
      allowed_clients = [aws_cognito_user_pool_client.m2m.id]
    }
  }

  protocol_configuration {
    server_protocol = "MCP"
  }

  request_header_configuration {
    request_header_allowlist = ["Authorization"]
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  depends_on = [aws_iam_role_policy.agentcore_runtime_base]
}
