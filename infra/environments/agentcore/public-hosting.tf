# kics-scan disable=00e5e55e-c2ff-46b3-a757-a7a1cd802456,084c6686-2a70-4710-91b1-000393e54c12,0afa6ab8-a047-48cf-be07-93a2f8c34cf7,3a1e94df-6847-4c0e-a3b6-6c6af4e128ef,3e34db4f-0ad9-4290-bfd0-4a9ee884acaf,4bc4dd4c-7d8d-405e-a0fb-57fa4c31b4d9,568a4d22-3517-44a6-a7ad-6a7eed88722c,94690d79-b3b0-43de-b656-84ebef5753e5,afecd1f1-6378-4f7e-bb3b-60c35801fdd4,bafe7989-3c4b-47f0-910b-e6e1cba7f146,e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   00e5e55e: false positive: the distribution requires an ACM certificate with TLSv1.2_2021 (no default certificate)
#   084c6686: Shield Advanced is an org-level subscription; Shield Standard applies
#   0afa6ab8: internal ALB, reachable only through the CloudFront VPC origin; WAF is on CloudFront
#   3a1e94df: false positive: custom domain + ACM certificate are required (portal_domain_name / portal_acm_certificate_arn)
#   3e34db4f: false positive: ALB access logs go to aws_s3_bucket.alb_logs
#   4bc4dd4c: false positive: the policy has a DenyInsecureTransport statement
#   568a4d22: false positive: versioning is set by aws_s3_bucket_versioning.site
#   94690d79: CloudFront logging deferred to a later hardening phase
#   afecd1f1: deletion protection would block terraform destroy / teardown
#   bafe7989: API tasks need a public IP for ECR/AWS egress (no NAT); their SG admits only the internal ALB
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f861041c: access logging needs a log bucket; deferred / adopting team's choice
# ══════════════════════════════════════════════════════════════════════
# Public hosting — the missing front door.
#
# Everything else in this environment is reachable only from inside AWS: the
# API task definition exists but nothing runs it, and there is no load
# balancer or CDN, so `terraform apply` alone produces a stack with no URL.
# That is fine for local development (scripts/start.sh runs the API and UI on
# a laptop against these cloud resources) but means the environment cannot be
# demonstrated or shared.
#
# This file adds, and nothing else depends on it:
#   • an ECS service that actually runs the existing API task definition
#   • an INTERNAL ALB in two private subnets (no internet route), reached only
#     through a CloudFront VPC origin — the load balancer has no public address
#   • an S3 website bucket + CloudFront distribution for the React app, served on
#     YOUR domain with an ACM certificate and a TLSv1.2_2021 minimum (the default
#     *.cloudfront.net certificate is not allowed: it permits TLSv1)
#   • CloudFront -> ALB for /api/* so the browser sees one origin (no CORS)
#   • ALB access logs in a private, TLS-only S3 bucket
#
# Access control: an optional WAF viewer-IP allowlist (cloudfront_allowed_cidrs) at the
# edge, plus Cognito (enable_cognito) — the API rejects any request without a valid JWT.
#
# This is a proof-of-concept front door for demos with SYNTHETIC data. It is not a
# production deployment pattern; see README.md "Important: proof of concept".
#
# Gated behind var.enable_public_hosting (default false) so it is opt-in and
# `terraform apply` behaves exactly as before unless you ask for it.
# ══════════════════════════════════════════════════════════════════════

variable "enable_public_hosting" {
  description = "Create the ECS service, ALB and CloudFront distribution that make this environment reachable from a browser."
  type        = bool
  default     = false
}

variable "api_desired_count" {
  description = "Number of API tasks to run."
  type        = number
  default     = 1
}

variable "alb_idle_timeout_seconds" {
  description = "API ALB idle timeout (seconds). Chat waits on the Harness (tool calls), so it is longer than the 60s AWS default."
  type        = number
  default     = 120
}

variable "cloudfront_origin_read_timeout_seconds" {
  description = "CloudFront -> ALB origin read timeout (seconds). 60 is the CloudFront max without a quota increase (AWS default 30); Harness chat responses routinely exceed 30s."
  type        = number
  default     = 60
}

variable "cloudfront_allowed_cidrs" {
  description = "Viewer IPv4 CIDRs allowed through CloudFront (AWS WAF allowlist; everyone else gets 403). Empty = no WAF (Cognito only)."
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.cloudfront_allowed_cidrs, "0.0.0.0/0")
    error_message = "cloudfront_allowed_cidrs must not contain 0.0.0.0/0 — leave it empty to disable the allowlist."
  }
}

variable "portal_domain_name" {
  description = "Custom domain the portal is served on (e.g. amf.example.com). Required when enable_public_hosting = true; you create the DNS record (CNAME/alias to the distribution)."
  type        = string
  default     = ""
}

variable "portal_acm_certificate_arn" {
  description = "ACM certificate ARN in us-east-1 covering portal_domain_name. Required when enable_public_hosting = true (CloudFront's default certificate allows TLSv1)."
  type        = string
  default     = ""
  validation {
    condition     = var.portal_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:us-east-1:[0-9]{12}:certificate/", var.portal_acm_certificate_arn))
    error_message = "portal_acm_certificate_arn must be an ACM certificate in us-east-1 (CloudFront requirement)."
  }
}

variable "cloudfront_minimum_protocol_version" {
  description = "Minimum viewer TLS version for the portal distribution."
  type        = string
  default     = "TLSv1.2_2021"
}

variable "alb_log_retention_days" {
  description = "Days to keep ALB access logs."
  type        = number
  default     = 90
}

locals {
  hosting     = var.enable_public_hosting ? 1 : 0
  viewer_waf  = var.enable_public_hosting && length(var.cloudfront_allowed_cidrs) > 0 ? 1 : 0
  site_bucket = "${var.project_prefix}-frontend-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  alb_logs    = "${var.project_prefix}-alb-logs-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

# ── Private subnets for the internal ALB (no internet route) ──────────

resource "aws_subnet" "private_alb" {
  count             = local.hosting * 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, 20 + count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${var.project_prefix}-private-alb-${count.index}", Tier = "private" }
}

resource "aws_route_table" "private_alb" {
  count  = local.hosting
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_prefix}-private-alb-rt" }
}

resource "aws_route_table_association" "private_alb" {
  count          = local.hosting * 2
  subnet_id      = aws_subnet.private_alb[count.index].id
  route_table_id = aws_route_table.private_alb[0].id
}

# ── Security groups ───────────────────────────────────────────────────
# The internal ALB accepts traffic only from CloudFront's origin-facing prefix list,
# which is how CloudFront VPC origins reach it; it has no public address at all.

data "aws_ec2_managed_prefix_list" "cloudfront" {
  count = local.hosting
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  count       = local.hosting
  name        = "${var.project_prefix}-alb-sg"
  description = "Internal ALB ingress from the CloudFront VPC origin only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "CloudFront origin-facing ranges"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront[0].id]
  }

  egress {
    description = "ALB to the API tasks inside the VPC only"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = { Name = "${var.project_prefix}-alb-sg" }
}

resource "aws_security_group" "api_task" {
  count       = local.hosting
  name        = "${var.project_prefix}-api-task-sg"
  description = "API tasks accept traffic from the ALB only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "ALB to API container"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb[0].id]
  }

  egress {
    description = "Allow all outbound to AWS service endpoints (ECR, CloudWatch Logs, etc.)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_prefix}-api-task-sg" }
}

# ── Load balancer ─────────────────────────────────────────────────────

resource "aws_lb" "api" {
  #checkov:skip=CKV2_AWS_20:internal ALB behind a CloudFront VPC origin; viewers get HTTPS at CloudFront, the hop is in-VPC
  #checkov:skip=CKV2_AWS_28:internal ALB reachable only via the CloudFront VPC origin; WAF is on CloudFront
  #checkov:skip=CKV_AWS_150:deletion protection would block terraform destroy of a PoC estate
  count                      = local.hosting
  name                       = "${var.project_prefix}-api-alb"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb[0].id]
  subnets                    = aws_subnet.private_alb[*].id
  drop_invalid_header_fields = true

  access_logs {
    bucket  = aws_s3_bucket.alb_logs[0].id
    prefix  = "api-alb"
    enabled = true
  }
  # Chat waits on the Harness (tool calls) — longer than the 60s default.
  idle_timeout = var.alb_idle_timeout_seconds
  tags         = { Name = "${var.project_prefix}-api-alb" }
}

resource "aws_lb_target_group" "api" {
  #checkov:skip=CKV_AWS_378:internal ALB to the API task, both inside the VPC (no internet path)
  count       = local.hosting
  name        = "${var.project_prefix}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # Fargate awsvpc tasks register by IP, not instance

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Let a replacement register before the old target is removed.
  deregistration_delay = 30
}

# The listener is plain HTTP on an INTERNAL load balancer: the only client is the
# CloudFront VPC origin (ENIs inside this VPC), viewers terminate TLSv1.2+ at CloudFront,
# and the ALB has no public address or internet route, so no TLS policy applies here.
resource "aws_lb_listener" "api" {
  #checkov:skip=CKV_AWS_103:internal ALB reachable only by the CloudFront VPC origin; viewer TLS terminates at CloudFront
  #checkov:skip=CKV_AWS_2:internal ALB reachable only by the CloudFront VPC origin; viewer TLS terminates at CloudFront
  count             = local.hosting
  load_balancer_arn = aws_lb.api[0].arn
  port              = 80
  # nosemgrep: terraform.aws.security.insecure-load-balancer-tls-version.insecure-load-balancer-tls-version
  protocol = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
}

# ── The ECS service that was missing ──────────────────────────────────

resource "aws_ecs_service" "api" {
  count           = local.hosting
  name            = "${var.project_prefix}-api-service"
  cluster         = module.ecs.cluster_id
  task_definition = module.worker_task.api_task_definition_arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  # Tasks carry the service's tags (default_tags) and the ECS-managed cluster/service tags.
  propagate_tags          = "SERVICE"
  enable_ecs_managed_tags = true

  network_configuration {
    subnets = aws_subnet.public[*].id
    # Public IP is required for image pull without a NAT gateway.
    assign_public_ip = true
    security_groups  = [aws_security_group.api_task[0].id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api[0].arn
    container_name   = var.worker_container_name # the API reuses the worker container
    container_port   = 8000
  }

  # The task definition is rolled by CI/CD (or by hand) outside Terraform;
  # ignoring it stops every apply from reverting the running revision.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
    # Hosted mode is never anonymous: the API must validate Cognito
    # JWTs, so a public deployment refuses to plan with Cognito off or the dev-auth bypass on.
    precondition {
      condition     = var.enable_cognito && lower(trimspace(var.dev_auth)) == "false"
      error_message = "enable_public_hosting = true requires enable_cognito = true and dev_auth = \"false\" (setup.sh: ENABLE_PUBLIC_HOSTING=true implies ENABLE_COGNITO=true; don't set DEV_AUTH for hosted)."
    }
  }

  depends_on = [aws_lb_listener.api]
}

# ── ALB access logs (private, TLS-only, SSE-S3 as ALB log delivery requires) ──

data "aws_elb_service_account" "current" {}

resource "aws_s3_bucket" "alb_logs" {
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  #checkov:skip=CKV_AWS_144:single-region PoC; DR replication not in scope
  #checkov:skip=CKV_AWS_145:ALB access-log delivery supports SSE-S3 only
  #checkov:skip=CKV_AWS_18:this IS the access-log bucket
  count         = local.hosting
  bucket        = local.alb_logs
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  count                   = local.hosting
  bucket                  = aws_s3_bucket.alb_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "alb_logs" {
  count  = local.hosting
  bucket = aws_s3_bucket.alb_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  count  = local.hosting
  bucket = aws_s3_bucket.alb_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  count      = local.hosting
  bucket     = aws_s3_bucket.alb_logs[0].id
  depends_on = [aws_s3_bucket_versioning.alb_logs]
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = var.alb_log_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.alb_log_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = var.s3_abort_incomplete_upload_days
    }
  }
}

resource "aws_s3_bucket_policy" "alb_logs" {
  count  = local.hosting
  bucket = aws_s3_bucket.alb_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ElbLogDelivery"
      Effect    = "Allow"
      Principal = { AWS = data.aws_elb_service_account.current.arn }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.alb_logs[0].arn}/api-alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
      }, {
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.alb_logs[0].arn, "${aws_s3_bucket.alb_logs[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ── CloudFront VPC origin: the only path to the internal ALB ───────────

resource "aws_cloudfront_vpc_origin" "api" {
  count = local.hosting
  vpc_origin_endpoint_config {
    name                   = "${var.project_prefix}-api-alb"
    arn                    = aws_lb.api[0].arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

# ── Frontend bucket (private; CloudFront reads it via OAC) ─────────────

resource "aws_s3_bucket" "site" {
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  #checkov:skip=CKV_AWS_144:single-region demo; DR replication not in scope
  #checkov:skip=CKV_AWS_145:SSE-S3 at rest; CMK deferred to a later hardening phase
  #checkov:skip=CKV_AWS_18:access logging deferred to a later hardening phase
  count  = local.hosting
  bucket = local.site_bucket
}

resource "aws_s3_bucket_public_access_block" "site" {
  count                   = local.hosting
  bucket                  = aws_s3_bucket.site[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "site" {
  count  = local.hosting
  bucket = aws_s3_bucket.site[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

# Each UI deploy (`aws s3 sync --delete`) leaves the previous build as noncurrent versions.
resource "aws_s3_bucket_lifecycle_configuration" "site" {
  count      = local.hosting
  bucket     = aws_s3_bucket.site[0].id
  depends_on = [aws_s3_bucket_versioning.site]
  rule {
    id     = "expire-old-builds"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.site_noncurrent_version_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = var.s3_abort_incomplete_upload_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  count  = local.hosting
  bucket = aws_s3_bucket.site[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "site" {
  count                             = local.hosting
  name                              = "${var.project_prefix}-site-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "site" {
  count  = local.hosting
  bucket = aws_s3_bucket.site[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site[0].arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.site[0].arn
        }
      }
      }, {
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.site[0].arn, "${aws_s3_bucket.site[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Rewrite client-side routes at the viewer request, before S3 is called. API and
# health paths are deliberately excluded so their HTTP errors remain JSON/5xx
# instead of being masked as a successful index.html response.
resource "aws_cloudfront_function" "spa_rewrite" {
  count   = local.hosting
  name    = "${var.project_prefix}-spa-rewrite"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      if (uri.startsWith('/api/') || uri.startsWith('/health')) {
        return request;
      }
      var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
      if (uri.endsWith('/') || lastSegment.indexOf('.') === -1) {
        request.uri = '/index.html';
      }
      return request;
    }
  EOT
}

# ── WAF: viewer IP allowlist (scope CLOUDFRONT lives in us-east-1) ─────
# Requests from outside cloudfront_allowed_cidrs are blocked at the edge, before the
# SPA or the API (which additionally requires a Cognito JWT).

resource "aws_wafv2_ip_set" "viewers" {
  count              = local.viewer_waf
  name               = "${var.project_prefix}-portal-viewers"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses          = var.cloudfront_allowed_cidrs
}

resource "aws_wafv2_web_acl" "portal" {
  #checkov:skip=CKV2_AWS_31:WAF logging deferred to a later hardening phase
  count = local.viewer_waf
  name  = "${var.project_prefix}-portal"
  scope = "CLOUDFRONT"

  default_action {
    block {}
  }

  rule {
    name     = "allow-listed-viewers"
    priority = 0
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.viewers[0].arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_prefix}-portal-allowed"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_prefix}-portal"
    sampled_requests_enabled   = true
  }
}

# ── Security response headers ────────────────────
# CSP for the React/CloudScape SPA: scripts only from the bundle; inline styles are
# needed by CloudScape/React style props and mermaid's generated SVG <style>; the only
# cross-origin call is the Cognito Hosted UI token exchange (oauth2/token). The API is
# same-origin (/api/*). Empty portal_csp = the default below.

variable "portal_csp" {
  description = "Content-Security-Policy for the hosted portal (CloudFront). Empty = the built-in SPA policy."
  type        = string
  default     = ""
}

locals {
  portal_csp_default = join("; ", [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self' https://*.amazoncognito.com https://cognito-idp.${var.aws_region}.amazonaws.com",
    "frame-src 'self' blob: data:",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self' https://*.amazoncognito.com",
  ])
  portal_csp = var.portal_csp != "" ? var.portal_csp : local.portal_csp_default
}

resource "aws_cloudfront_response_headers_policy" "portal_security" {
  count   = local.hosting
  name    = "${var.project_prefix}-portal-security-headers"
  comment = "AMF portal CSP + HSTS + nosniff + frame DENY + referrer policy"

  security_headers_config {
    content_security_policy {
      content_security_policy = local.portal_csp
      override                = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }
}

# ── CloudFront: one origin for the SPA and the API ─────────────────────

resource "aws_cloudfront_distribution" "site" {
  #checkov:skip=CKV2_AWS_47:WAF is an IP allowlist in front of a Python API (no Log4j); managed rules in a later hardening phase
  count               = local.hosting
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.project_prefix} AMF portal"
  web_acl_id          = local.viewer_waf > 0 ? aws_wafv2_web_acl.portal[0].arn : null
  aliases             = compact([var.portal_domain_name])

  origin {
    domain_name              = aws_s3_bucket.site[0].bucket_regional_domain_name
    origin_id                = "s3-site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site[0].id
  }

  origin {
    domain_name = aws_lb.api[0].dns_name
    origin_id   = "alb-api"
    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.api[0].id
      # 60s is the CloudFront max without a quota increase (default 30s) — chat
      # responses from the Harness routinely exceed 30s.
      origin_read_timeout = var.cloudfront_origin_read_timeout_seconds
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-site"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # CachingOptimized
    cache_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.portal_security[0].id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite[0].arn
    }
  }

  # Serving the API from the same host as the SPA means the browser makes
  # same-origin requests, so no CORS configuration is needed at all.
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "alb-api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # CachingDisabled + AllViewerExceptHostHeader
    cache_policy_id            = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    origin_request_policy_id   = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.portal_security[0].id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = var.portal_acm_certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = var.cloudfront_minimum_protocol_version
  }

  price_class = "PriceClass_100"

  lifecycle {
    precondition {
      condition     = var.portal_domain_name != "" && var.portal_acm_certificate_arn != ""
      error_message = "enable_public_hosting = true requires portal_domain_name and portal_acm_certificate_arn (us-east-1 ACM certificate): the default CloudFront certificate allows TLSv1 (setup.sh: PORTAL_DOMAIN_NAME / PORTAL_ACM_CERTIFICATE_ARN)."
    }
    precondition {
      condition     = startswith(var.cloudfront_minimum_protocol_version, "TLSv1.2") || startswith(var.cloudfront_minimum_protocol_version, "TLSv1.3")
      error_message = "cloudfront_minimum_protocol_version must be TLSv1.2_* or newer."
    }
  }
}

# ── Outputs ───────────────────────────────────────────────────────────

output "portal_url" {
  description = "Public URL of the AMF portal (null unless enable_public_hosting=true)."
  value       = local.hosting > 0 ? "https://${var.portal_domain_name}" : null
}

output "site_bucket_name" {
  description = "Bucket to sync the built frontend into."
  value       = local.hosting > 0 ? aws_s3_bucket.site[0].bucket : null
}

output "cloudfront_distribution_id" {
  description = "Distribution to invalidate after a frontend deploy."
  value       = local.hosting > 0 ? aws_cloudfront_distribution.site[0].id : null
}

output "portal_cloudfront_domain_name" {
  description = "Distribution hostname — point the portal_domain_name DNS record (CNAME / Route 53 alias) at it."
  value       = local.hosting > 0 ? aws_cloudfront_distribution.site[0].domain_name : null
}

output "api_alb_dns_name" {
  description = "Internal ALB hostname (reachable only through the CloudFront VPC origin)."
  value       = local.hosting > 0 ? aws_lb.api[0].dns_name : null
}
