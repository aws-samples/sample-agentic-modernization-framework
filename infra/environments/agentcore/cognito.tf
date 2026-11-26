# kics-scan disable=baee238e-1921-4801-9c3f-79ae1d7b2cbc,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   baee238e: false positive: token_validity_units values ("minutes"/"days"), not secrets
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# ══════════════════════════════════════════════════════════════════════
# Cognito — the portal's identity provider.
#
# The React SPA authenticates against the Cognito Hosted UI using the
# Authorization Code + PKCE flow (frontend/src/auth/cognito.ts), exchanges the
# code for tokens, and sends the access token as a Bearer JWT. The FastAPI
# control plane validates the JWT (backend/src/auth/cognito.py) and maps the
# user's Cognito group to a role (backend/src/auth/rbac.py):
#   Admin  > Engineer > Viewer
# A seed user in the Admin group can do everything.
#
# Gated behind var.enable_cognito so a plain `terraform apply` (local dev, where
# DEV_AUTH bypasses login) is unchanged. UAT sets enable_cognito=true together
# with enable_public_hosting so the callback URLs include the CloudFront domain.
# ══════════════════════════════════════════════════════════════════════

variable "enable_cognito" {
  description = "Create the Cognito user pool, app client, groups and seed admin user for the portal."
  type        = bool
  default     = false
}

variable "cognito_mfa" {
  description = "Portal MFA (TOTP): OPTIONAL (default, sandbox PoC) or ON (every user must enrol; recommended for a shared hosted demo)."
  type        = string
  default     = "OPTIONAL"
  validation {
    condition     = contains(["OPTIONAL", "ON"], var.cognito_mfa)
    error_message = "cognito_mfa must be OPTIONAL or ON."
  }
}

variable "admin_email" {
  description = "Email (and username) of the seed Cognito admin user, added to the Admin group."
  type        = string
  default     = "admin@example.com"
}

variable "cognito_access_token_validity_minutes" {
  description = "Portal access-token lifetime (minutes)."
  type        = number
  default     = 60
}

variable "cognito_id_token_validity_minutes" {
  description = "Portal ID-token lifetime (minutes)."
  type        = number
  default     = 60
}

variable "cognito_refresh_token_validity_days" {
  description = "Portal refresh-token lifetime (days)."
  type        = number
  default     = 30
}

variable "extra_callback_urls" {
  description = "Extra OAuth callback/logout URLs (e.g. a custom domain). The CloudFront URL (when public hosting is on) and http://localhost:3000 are always included."
  type        = list(string)
  default     = []
}

locals {
  cognito = var.enable_cognito ? 1 : 0

  # The SPA redirect target is the CloudFront portal when hosting is on, plus
  # localhost for running the UI on a laptop against these cloud resources.
  cognito_urls = distinct(concat(
    ["http://localhost:3000"],
    local.hosting > 0 ? ["https://${var.portal_domain_name}"] : [],
    var.extra_callback_urls,
  ))
}

# ── User pool ─────────────────────────────────────────────────────────

resource "aws_cognito_user_pool" "portal" {
  count = local.cognito
  name  = "${var.project_prefix}-portal"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # TOTP authenticator MFA. OPTIONAL (default) keeps a sandbox PoC simple; set
  # cognito_mfa = "ON" for any hosted demo that other people sign in to.
  mfa_configuration = var.cognito_mfa
  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # Portal users are provisioned by an admin (no open self sign-up).
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = { Name = "${var.project_prefix}-portal" }
}

resource "aws_cognito_user_pool_domain" "portal" {
  count        = local.cognito
  domain       = "${var.project_prefix}-amf-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.portal[0].id
}

# ── App client (public SPA — no secret, PKCE) ─────────────────────────

resource "aws_cognito_user_pool_client" "spa" {
  count        = local.cognito
  name         = "${var.project_prefix}-portal-spa"
  user_pool_id = aws_cognito_user_pool.portal[0].id

  # SPAs cannot keep a secret; PKCE protects the code exchange instead.
  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = local.cognito_urls
  logout_urls   = local.cognito_urls

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  access_token_validity  = var.cognito_access_token_validity_minutes
  id_token_validity      = var.cognito_id_token_validity_minutes
  refresh_token_validity = var.cognito_refresh_token_validity_days
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # Don't leak whether an account exists on failed logins.
  prevent_user_existence_errors = "ENABLED"
}

# ── Groups (mapped to roles in backend/src/auth/rbac.py) ──────────────

resource "aws_cognito_user_group" "admin" {
  count        = local.cognito
  name         = "Admin"
  user_pool_id = aws_cognito_user_pool.portal[0].id
  description  = "Full access — manage workflows, approve gates, configure settings"
  precedence   = 1
}

resource "aws_cognito_user_group" "engineer" {
  count        = local.cognito
  name         = "Engineer"
  user_pool_id = aws_cognito_user_pool.portal[0].id
  description  = "Migration engineers — submit inputs, approve steps, view reports"
  precedence   = 2
}

resource "aws_cognito_user_group" "viewer" {
  count        = local.cognito
  name         = "Viewer"
  user_pool_id = aws_cognito_user_pool.portal[0].id
  description  = "Read-only access — view dashboards and reports"
  precedence   = 3
}

# ── Seed admin user ───────────────────────────────────────────────────
# Created with a generated temporary password (Cognito forces a reset on first
# login). The password is a sensitive Terraform output; setup.sh surfaces it.

resource "random_password" "admin" {
  count            = local.cognito
  length           = 16
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "!@#%^*-_"
}

resource "aws_cognito_user" "admin" {
  count        = local.cognito
  user_pool_id = aws_cognito_user_pool.portal[0].id
  username     = var.admin_email

  attributes = {
    email          = var.admin_email
    email_verified = "true"
  }

  temporary_password = random_password.admin[0].result
  message_action     = "SUPPRESS" # no email delivery dependency; surfaced via output
}

resource "aws_cognito_user_in_group" "admin" {
  count        = local.cognito
  user_pool_id = aws_cognito_user_pool.portal[0].id
  group_name   = aws_cognito_user_group.admin[0].name
  username     = aws_cognito_user.admin[0].username
}

# ── Outputs (consumed by setup.sh → backend .env + frontend build) ────

output "cognito_user_pool_id" {
  description = "Cognito user pool id (backend COGNITO_USER_POOL_ID)."
  value       = local.cognito > 0 ? aws_cognito_user_pool.portal[0].id : null
}

output "cognito_app_client_id" {
  description = "SPA app client id (backend COGNITO_APP_CLIENT_ID / frontend VITE_COGNITO_CLIENT_ID)."
  value       = local.cognito > 0 ? aws_cognito_user_pool_client.spa[0].id : null
}

output "cognito_hosted_ui_domain" {
  description = "Hosted UI base URL (frontend VITE_COGNITO_DOMAIN)."
  value       = local.cognito > 0 ? "https://${aws_cognito_user_pool_domain.portal[0].domain}.auth.${var.aws_region}.amazoncognito.com" : null
}

output "cognito_admin_username" {
  description = "Seed admin username (email)."
  value       = local.cognito > 0 ? aws_cognito_user.admin[0].username : null
}

output "cognito_admin_temp_password" {
  description = "Temporary password for the seed admin (reset required on first login)."
  value       = local.cognito > 0 ? random_password.admin[0].result : null
  sensitive   = true
}
