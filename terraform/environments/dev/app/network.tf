data "aws_network_interface" "wireguard" {
  id = var.wireguard_eni_id
}

data "aws_subnet" "lambda" {
  for_each = toset(var.lambda_subnet_ids)

  id = each.value
}

locals {
  lambda_subnet_cidrs = [for subnet in data.aws_subnet.lambda : subnet.cidr_block]

  # Host and port of the on-prem vLLM endpoint (the variable is validated to be
  # http://<ipv4>:<port>). The Lambda reaches it through the router.
  onprem_vllm      = regex("^http://(?P<host>[0-9.]+):(?P<port>[0-9]+)$", var.onprem_vllm_base_url)
  onprem_vllm_port = tonumber(local.onprem_vllm.port)

  # Ports the Lambda may send through the router: HTTPS to the AWS APIs and the on-prem vLLM port.
  router_ports = [443, local.onprem_vllm_port]

  router_ingress = {
    for pair in setproduct(local.lambda_subnet_cidrs, local.router_ports) : "${pair[0]}-${pair[1]}" => {
      cidr = pair[0]
      port = pair[1]
    }
  }
}

resource "aws_security_group" "gateway_lambda" {
  name        = "${local.name}-gateway-lambda"
  description = "Gateway Lambda: outbound only"
  vpc_id      = var.vpc_id

  lifecycle {
    precondition {
      condition = alltrue(concat(
        [data.aws_network_interface.wireguard.vpc_id == var.vpc_id],
        [for subnet in data.aws_subnet.lambda : subnet.vpc_id == var.vpc_id]
      ))
      error_message = "The router ENI and the Lambda subnets must be in var.vpc_id."
    }
  }
}

resource "aws_vpc_security_group_egress_rule" "gateway_lambda_https" {
  security_group_id = aws_security_group.gateway_lambda.id
  description       = "AWS APIs through the router"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# The on-prem vLLM endpoint, through the router. The Lambda subnet's route table
# already sends 10.100.0.3/32 to the router ENI (managed outside Terraform, see
# the removed blocks below), so only the security group side is managed here.
resource "aws_vpc_security_group_egress_rule" "gateway_lambda_onprem_vllm" {
  security_group_id = aws_security_group.gateway_lambda.id
  description       = "On-prem vLLM through the router"
  ip_protocol       = "tcp"
  from_port         = local.onprem_vllm_port
  to_port           = local.onprem_vllm_port
  cidr_ipv4         = "${local.onprem_vllm.host}/32"
}

removed {
  from = aws_route_table.gateway_lambda

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route.default_via_router

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route.gpu_via_router

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_route_table_association.gateway_lambda

  lifecycle {
    destroy = false
  }
}

# Forwarded traffic reaches the router with the Lambda ENI's address as
# source, so these rules allow the Lambda subnet CIDR.
resource "aws_vpc_security_group_ingress_rule" "router_from_lambda" {
  for_each = local.router_ingress

  security_group_id = one(data.aws_network_interface.wireguard.security_groups)
  description       = "Gateway Lambda subnet to router, tcp ${each.value.port}"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}
