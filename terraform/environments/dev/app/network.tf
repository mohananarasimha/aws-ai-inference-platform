data "aws_network_interface" "wireguard" {
  id = var.wireguard_eni_id
}

data "aws_subnet" "lambda" {
  for_each = toset(var.lambda_subnet_ids)

  id = each.value
}

locals {
  lambda_subnet_cidrs = [for subnet in data.aws_subnet.lambda : subnet.cidr_block]

  router_ingress = {
    for cidr in local.lambda_subnet_cidrs : "${cidr}-443" => {
      cidr = cidr
      port = 443
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
