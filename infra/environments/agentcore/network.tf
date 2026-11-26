# kics-scan disable=52f04a44-6bfa-4c41-b1d3-4ae99a2de05c,e592a0c5-5bdb-414c-9066-5dba7cdea370,f83121ea-03da-434f-9277-9cd247ab3047,fd632aaf-b8a1-424d-a4d1-0de22fd3247a
# Accepted kics findings for this file (scanner suppressions; reasons):
#   52f04a44: public subnets (IGW route) for worker/API egress without NAT; map_public_ip_on_launch is off
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f83121ea: VPC flow logs deferred / added by the landing zone
#   fd632aaf: Network Firewall is a landing-zone control, not per app
# ══════════════════════════════════════════════════════════════════════
# Networking — minimal VPC with 2 public subnets for the ATX Fargate worker.
#
# The ATX worker is launched by the execution tool Lambda with
# assignPublicIp=ENABLED, so public subnets + an Internet Gateway are enough
# (no NAT gateway needed). Makes the environment standalone — it no longer
# depends on the dev environment's VPC/subnets.
# ══════════════════════════════════════════════════════════════════════

variable "vpc_cidr" {
  description = "CIDR of this environment's VPC. Also the only source allowed to reach the worker/API task on port 8000 (worker-task SG)."
  type        = string
  default     = "10.30.0.0/16"
  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:AZs are picked by index; zone pinning not needed for a 2-subnet VPC
  state = "available"
}

resource "aws_vpc" "main" {
  #checkov:skip=CKV2_AWS_11:VPC flow logs deferred to a later hardening phase (demo VPC, no NAT)
  #checkov:skip=CKV2_AWS_12:default SG is unused; every resource gets an explicit SG
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project_prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_prefix}-igw" }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  # Nothing gets a public IP by default: the ATX worker (assignPublicIp, ECS_ASSIGN_PUBLIC_IP) and the
  # hosted API service (assign_public_ip) request one explicitly for egress, since there is no NAT.
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.project_prefix}-public-${count.index}", Tier = "public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.project_prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}
output "public_subnet_ids" {
  description = "Public subnet IDs"
  value       = aws_subnet.public[*].id
}
