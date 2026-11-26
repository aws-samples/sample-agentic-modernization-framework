# kics-scan disable=084c6686-2a70-4710-91b1-000393e54c12,52f04a44-6bfa-4c41-b1d3-4ae99a2de05c,e592a0c5-5bdb-414c-9066-5dba7cdea370,f83121ea-03da-434f-9277-9cd247ab3047,fd632aaf-b8a1-424d-a4d1-0de22fd3247a
# Accepted kics findings for this file (scanner suppressions; reasons):
#   084c6686: Shield Advanced is an org-level subscription; Shield Standard applies
#   52f04a44: public subnets by design (internet-facing ALB / NAT only; no auto-assigned public IPs)
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f83121ea: VPC flow logs deferred / added by the landing zone
#   fd632aaf: Network Firewall is a landing-zone control, not per app
################################################################################
# VPC Module — Standard multi-AZ architecture (az_count AZs, default 2)
# Naming: {app_id}-vpc, {app_id}-subnet-public-{az}, etc.
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "vpc_cidr" {
  description = "VPC CIDR"
  type        = string
  default     = "10.0.0.0/16"
}
variable "azs" {
  type        = list(string)
  default     = []
  description = "Explicit AZ names. Empty = the first az_count available AZs of the provider region."
}
variable "az_count" {
  description = "AZ count"
  type        = number
  default     = 2
  validation {
    condition     = var.az_count >= 1 && var.az_count <= 6
    error_message = "az_count must be between 1 and 6."
  }
}
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}

data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:AZs are picked by index
  state = "available"
}

locals {
  azs = length(var.azs) > 0 ? var.azs : slice(data.aws_availability_zones.available.names, 0, var.az_count)
  # Subnet layout. /16–/20 keeps the original /+8 layout (public 1.., app 11.., db 21..) so existing VPCs
  # never move. Smaller VPCs (/21–/24) get a compact layout (public 0.., app n.., db 2n..) with /+3 subnets,
  # or /+4 when 3 tiers × AZs need more than 8 slots; a /24 then yields /27 (or /28) subnets. AWS minimum
  # is /28; the assessment gate only accepts /16–/24 (core/strategy_options.VPC_CIDR_RE).
  vpc_prefix  = tonumber(split("/", var.vpc_cidr)[1])
  compact     = local.vpc_prefix > 20
  az_n        = length(local.azs)
  subnet_bits = local.compact ? (local.az_n * 3 > 8 ? 4 : 3) : 8
  public_base = local.compact ? 0 : 1
  app_base    = local.compact ? local.az_n : 11
  db_base     = local.compact ? 2 * local.az_n : 21
  name_prefix = var.app_id
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
}

resource "aws_vpc" "main" {
  #checkov:skip=CKV2_AWS_11:flow logs are added by the landing zone / adopting team
  #checkov:skip=CKV2_AWS_12:default SG is unused; every resource gets an explicit SG
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.common_tags, { Name = "${local.name_prefix}-vpc" })
}

# Public subnets (internet-facing ALB, NAT). No auto-assigned public IPs: the
# ALB and the NAT gateway (EIP) get theirs explicitly; workloads run in private subnets.
resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, local.subnet_bits, local.public_base + count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = merge(local.common_tags, { Name = "${local.name_prefix}-subnet-public-${local.azs[count.index]}" })
}

# Private app subnets (ECS/Lambda)
resource "aws_subnet" "private_app" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, local.subnet_bits, local.app_base + count.index)
  availability_zone = local.azs[count.index]
  tags              = merge(local.common_tags, { Name = "${local.name_prefix}-subnet-private-app-${local.azs[count.index]}" })
}

# Private DB subnets
resource "aws_subnet" "private_db" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, local.subnet_bits, local.db_base + count.index)
  availability_zone = local.azs[count.index]
  tags              = merge(local.common_tags, { Name = "${local.name_prefix}-subnet-private-db-${local.azs[count.index]}" })
}

# Internet Gateway
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.common_tags, { Name = "${local.name_prefix}-igw" })
}

# NAT Gateway (single, cost-optimized for non-prod; use per-AZ for prod)
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = merge(local.common_tags, { Name = "${local.name_prefix}-nat-eip" })
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = merge(local.common_tags, { Name = "${local.name_prefix}-nat" })
}

# Route tables
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-rt-public" })
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-rt-private" })
}

resource "aws_route_table_association" "public" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private_app" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.private_app[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_db" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.private_db[count.index].id
  route_table_id = aws_route_table.private.id
}

# Outputs
output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}
output "public_subnet_ids" {
  description = "Public subnet IDs"
  value       = aws_subnet.public[*].id
}
output "private_app_subnet_ids" {
  description = "Private app subnet IDs"
  value       = aws_subnet.private_app[*].id
}
output "private_db_subnet_ids" {
  description = "Private DB subnet IDs"
  value       = aws_subnet.private_db[*].id
}
output "nat_gateway_id" {
  description = "NAT gateway ID"
  value       = aws_nat_gateway.main.id
}
output "azs" {
  description = "AZs"
  value       = local.azs
}
