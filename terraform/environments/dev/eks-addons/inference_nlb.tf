# Applied after the app stack: the Lambda security group ID comes from its state.
data "terraform_remote_state" "app" {
  backend = "local"

  config = {
    path = "${path.module}/../app/terraform.tfstate"
  }
}

locals {
  lambda_security_group_id = data.terraform_remote_state.app.outputs.lambda_security_group_id
}

data "aws_vpc" "main" {
  id = var.vpc_id
}

# Frontend security group of the internal vLLM NLB (attached by the Service
# annotation). The load balancer controller adds the backend rules itself.
resource "aws_security_group" "inference_nlb" {
  name        = "${local.name}-inference-nlb"
  description = "Internal vLLM NLB: reachable from the gateway Lambda only"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${local.name}-inference-nlb"
  }
}

resource "aws_vpc_security_group_ingress_rule" "inference_nlb_from_lambda" {
  security_group_id            = aws_security_group.inference_nlb.id
  description                  = "Gateway Lambda to vLLM"
  ip_protocol                  = "tcp"
  from_port                    = var.inference_port
  to_port                      = var.inference_port
  referenced_security_group_id = local.lambda_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "inference_nlb_to_targets" {
  security_group_id = aws_security_group.inference_nlb.id
  description       = "NLB to vLLM pods and health checks"
  ip_protocol       = "tcp"
  from_port         = var.inference_port
  to_port           = var.inference_port
  cidr_ipv4         = data.aws_vpc.main.cidr_block
}

resource "aws_vpc_security_group_egress_rule" "gateway_lambda_inference_nlb" {
  security_group_id            = local.lambda_security_group_id
  description                  = "Gateway Lambda to the internal vLLM NLB"
  ip_protocol                  = "tcp"
  from_port                    = var.inference_port
  to_port                      = var.inference_port
  referenced_security_group_id = aws_security_group.inference_nlb.id
}
