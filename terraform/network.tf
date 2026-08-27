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
#
# tags is set explicitly (matching aws_security_group.bench above) rather than
# left for default_tags alone to fill in: with no tags argument at all, this
# resource's own tags_all lands in the plan as a static empty map rather than
# "known after apply", and default_tags then adds real values during apply.
# That mismatch is what "terraform apply" reported as a provider bug
# ("Provider produced inconsistent final plan ... tags_all: new element ...
# has appeared") and it failed the create outright. Confirmed fixed by hand:
# the same apply that fails without this creates cleanly with it.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.bench.id
  description       = "Container registries, GitHub, Bedrock, S3, SSM"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"

  tags = { Name = "${local.name}-egress" }
}
