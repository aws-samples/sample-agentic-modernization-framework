# kics-scan disable=45cff7b6-3b80-40c1-ba7b-2cf480678bb8,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   45cff7b6: Neptune is a re-derivable projection of DynamoDB; IAM auth, private subnets; audit logs deferred
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# ══════════════════════════════════════════════════════════════════════
# Amazon Neptune (opt-in: enable_neptune) — graph store for GRAPH_BACKEND=neptune.
#
# Neptune holds a projection of the DynamoDB portfolio table (the source of truth):
# applications, COMMUNICATES_WITH and SHARES_DATABASE edges. The API re-syncs it on a
# TTL (backend/src/db/neptune.py), so there is no data to back up — losing the cluster
# loses nothing (skip_final_snapshot).
#
# Private by construction: two subnets with no route to the internet, no public
# endpoint, IAM database auth, TLS (Neptune default), ingress only from the API task
# (and the worker) security groups on 8182.
# ══════════════════════════════════════════════════════════════════════

variable "graph_backend" {
  description = "Graph store the hosted API uses: dynamodb (derived from the table) | neptune (provisioned here) | none (wave planning off)."
  type        = string
  default     = "dynamodb"
  validation {
    condition     = contains(["dynamodb", "neptune", "none"], var.graph_backend)
    error_message = "graph_backend must be dynamodb, neptune or none."
  }
}

variable "enable_neptune" { # alias for graph_backend = "neptune"
  description = "Deprecated alias: same as graph_backend = \"neptune\"."
  type        = bool
  default     = false
}

variable "neptune_engine_version" {
  description = "Neptune engine version"
  type        = string
  default     = "1.4.8.1"
}

variable "neptune_min_capacity" {
  description = "NCUs; 1 is the smallest Serverless size"
  type        = number
  default     = 1
}

variable "neptune_max_capacity" {
  description = "Neptune max capacity"
  type        = number
  default     = 4
}

locals {
  neptune       = (var.enable_neptune || var.graph_backend == "neptune") ? 1 : 0
  graph_backend = local.neptune > 0 ? "neptune" : var.graph_backend
}

resource "aws_subnet" "private_graph" {
  count             = local.neptune * 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, 10 + count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${var.project_prefix}-private-graph-${count.index}", Tier = "private" }
}

# No routes beyond the VPC: Neptune needs no egress.
resource "aws_route_table" "private_graph" {
  count  = local.neptune
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_prefix}-private-graph-rt" }
}

resource "aws_route_table_association" "private_graph" {
  count          = local.neptune * 2
  subnet_id      = aws_subnet.private_graph[count.index].id
  route_table_id = aws_route_table.private_graph[0].id
}

resource "aws_neptune_subnet_group" "graph" {
  count      = local.neptune
  name       = "${var.project_prefix}-graph"
  subnet_ids = aws_subnet.private_graph[*].id
}

resource "aws_security_group" "neptune" {
  count       = local.neptune
  name        = "${var.project_prefix}-neptune-sg"
  description = "Neptune accepts 8182 from the AMF API task and worker only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "AMF API / worker to Neptune"
    from_port       = 8182
    to_port         = 8182
    protocol        = "tcp"
    security_groups = compact([local.hosting > 0 ? aws_security_group.api_task[0].id : "", module.worker_task.assessment_sg_id])
  }

  tags = { Name = "${var.project_prefix}-neptune-sg" }
}

resource "aws_neptune_cluster" "graph" {
  #checkov:skip=CKV2_AWS_58:Neptune is a re-derivable projection of DynamoDB (POST /graph/sync rebuilds it)
  count                               = local.neptune
  cluster_identifier                  = "${var.project_prefix}-graph"
  engine                              = "neptune"
  engine_version                      = var.neptune_engine_version
  neptune_subnet_group_name           = aws_neptune_subnet_group.graph[0].name
  vpc_security_group_ids              = [aws_security_group.neptune[0].id]
  iam_database_authentication_enabled = true
  storage_encrypted                   = true
  backup_retention_period             = 1
  skip_final_snapshot                 = true # a re-derivable projection of DynamoDB
  apply_immediately                   = true

  serverless_v2_scaling_configuration {
    min_capacity = var.neptune_min_capacity
    max_capacity = var.neptune_max_capacity
  }

  tags = { Name = "${var.project_prefix}-graph" }
}

resource "aws_neptune_cluster_instance" "graph" {
  count              = local.neptune
  identifier         = "${var.project_prefix}-graph-1"
  cluster_identifier = aws_neptune_cluster.graph[0].id
  engine             = "neptune"
  instance_class     = "db.serverless"
  apply_immediately  = true
}

# The hosted API runs as the worker task role (worker-task module): IAM DB auth.
resource "aws_iam_role_policy" "api_neptune" {
  count = local.neptune
  name  = "${var.project_prefix}-api-neptune"
  role  = element(split("/", module.worker_task.worker_task_role_arn), 1)
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["neptune-db:connect", "neptune-db:ReadDataViaQuery", "neptune-db:WriteDataViaQuery",
      "neptune-db:DeleteDataViaQuery", "neptune-db:GetQueryStatus", "neptune-db:CancelQuery"]
      Resource = "arn:${local.partition}:neptune-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${aws_neptune_cluster.graph[0].cluster_resource_id}/*"
    }]
  })
}

output "neptune_endpoint" {
  description = "Neptune cluster endpoint (GRAPH_BACKEND=neptune), null unless enable_neptune=true."
  value       = local.neptune > 0 ? aws_neptune_cluster.graph[0].endpoint : null
}
