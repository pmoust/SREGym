# No inbound rules at all. Access is SSM Session Manager, which dials out from
# the instance, so nothing needs to be reachable from the internet.
resource "aws_security_group" "bench" {
  name        = "${local.name}-sg"
  description = "SREGym benchmark fleet: egress only, SSM for access"
  vpc_id      = data.aws_vpc.default.id

  tags = { Name = "${local.name}-sg" }
}

# Egress is wide on purpose. The instance pulls container images from ghcr.io,
# docker.io, quay.io and registry.k8s.io, clones the mirror from GitHub, and
# talks to Bedrock, S3 and SSM. Enumerating those by CIDR would be a
# maintenance burden that buys little, since there is nothing inbound to reach.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.bench.id
  description       = "Container registries, GitHub, Bedrock, S3, SSM"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
