#!/usr/bin/env bash
set -euo pipefail
#
# AMF Setup — provisions the single AgentCore deployment.
#
# Everything is deployed through Terraform (infra/environments/agentcore) EXCEPT
# the Terraform remote-state S3 bucket and the DynamoDB lock table, which must be
# bootstrapped first. All AWS resources are named from a single prefix.
#
# Usage:
#   scripts/setup.sh [prefix]     # default prefix: amf
#
# Steps:
#   1. Prerequisites (python3, aws, docker, terraform)
#   2. .env + local Python deps (for running the backend API locally)
#   3. Bootstrap TF state bucket + lock table (the only out-of-Terraform resources)
#   4. terraform init + apply  → VPC, ECS, DynamoDB, S3, ECR, tool Lambdas,
#      MCP Gateway, Harness, and the Step Functions state machine
#   5. Build + push the ATX worker image to the (TF-created) ECR repo
#   6. Sync .env with Terraform outputs
#
# Application repos must be in AWS CodeCommit (the worker clones them with its task role).
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step()  { echo -e "${CYAN}[STEP]${NC} $1"; }

cd "$PROJECT_ROOT"

PREFIX="${1:-amf}"
TF_DIR="$PROJECT_ROOT/infra/environments/agentcore"

# ── Public hosting / auth toggles (opt-in; e.g. for a UAT deployment) ──
#   ENABLE_PUBLIC_HOSTING=true  → CloudFront + ALB + S3 site, builds & deploys the UI
#   ENABLE_COGNITO=true         → Cognito user pool + Admin group + seed admin (defaults to hosting)
#   ADMIN_EMAIL=you@corp.com    → seed admin username/email
#   GRAPH_BACKEND=dynamodb|neptune|none → graph store for the hosted API (default dynamodb):
#       neptune  → provisions Neptune Serverless (private subnets, IAM auth)
#       none     → wave planning off
#   ENABLE_NEPTUNE=true         → alias for GRAPH_BACKEND=neptune
#   ENABLE_DEPLOY=true          → create the deploy role; approving execution_review terraform-applies the
#                                 reviewed commit (default false: AMF applies nothing)
#   CLOUDFRONT_ALLOWED_CIDRS=a.b.c.d/32[,…] → WAF viewer-IP allowlist on CloudFront (others get 403)
#   PORTAL_DOMAIN_NAME=amf.example.com + PORTAL_ACM_CERTIFICATE_ARN=arn:aws:acm:us-east-1:…
#                               → REQUIRED with ENABLE_PUBLIC_HOSTING: the portal is served on your domain
#                                 with TLSv1.2+ (CloudFront's default certificate allows TLSv1). Point the
#                                 domain's DNS at the `portal_cloudfront_domain_name` output.
ENABLE_PUBLIC_HOSTING="${ENABLE_PUBLIC_HOSTING:-false}"
GRAPH_BACKEND="${GRAPH_BACKEND:-dynamodb}"
[[ "${ENABLE_NEPTUNE:-false}" == "true" ]] && GRAPH_BACKEND="neptune"
case "$GRAPH_BACKEND" in
  dynamodb|neptune|none) ;;
  *) echo "GRAPH_BACKEND must be dynamodb, neptune or none (got '$GRAPH_BACKEND')" >&2; exit 1 ;;
esac
CLOUDFRONT_ALLOWED_CIDRS="${CLOUDFRONT_ALLOWED_CIDRS:-}"
PORTAL_DOMAIN_NAME="${PORTAL_DOMAIN_NAME:-}"
PORTAL_ACM_CERTIFICATE_ARN="${PORTAL_ACM_CERTIFICATE_ARN:-}"
ENABLE_COGNITO="${ENABLE_COGNITO:-$ENABLE_PUBLIC_HOSTING}"
# Deploy gate: off by default (AMF stops at a reviewed migration branch). true = Admin approval applies it.
ENABLE_DEPLOY="${ENABLE_DEPLOY:-false}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"

# Update-or-append a KEY=VALUE line in a dotenv file (portable — no sed -i).
set_env() {
  local file="$1" key="$2" val="$3" tmp
  tmp="$(mktemp)"
  if [[ -f "$file" ]] && grep -q "^${key}=" "$file"; then
    grep -v "^${key}=" "$file" > "$tmp"
    mv "$tmp" "$file"
  fi
  echo "${key}=${val}" >> "$file"
}

echo ""
info "╔══════════════════════════════════════════════════════════════╗"
info "║  Agentic Modernization Framework — Setup                     ║"
info "║  Prefix: ${PREFIX}"
info "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Prerequisites ──────────────────────────────────────────────

step "1/6 — Checking prerequisites"
MISSING=0
REQUIRED_CMDS=(python3 aws docker terraform)
# The UI is only built/deployed when public hosting is enabled.
[[ "$ENABLE_PUBLIC_HOSTING" == "true" ]] && REQUIRED_CMDS+=(node pnpm)
for cmd in "${REQUIRED_CMDS[@]}"; do
  if command -v "$cmd" &>/dev/null; then info "  ✓ $cmd"; else error "  ✗ $cmd not found"; MISSING=1; fi
done
[[ "$MISSING" == "1" ]] && { error "Install the missing tools and re-run."; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGION="${AWS_REGION:-us-east-1}"
info "  ✓ AWS Account: ${ACCOUNT_ID} (Region: ${REGION})"

# ── Step 2: .env + Python deps ─────────────────────────────────────────

step "2/6 — Environment configuration + Python deps"
if [[ ! -f .env ]]; then cp .env.example .env; info "  ✓ .env created from example"; else info "  ✓ .env exists"; fi

if [[ ! -d backend/.venv ]]; then python3 -m venv backend/.venv; fi
# shellcheck disable=SC1091
source backend/.venv/bin/activate
pip install --upgrade pip -q
pip install -e ".[dev]" -q
info "  ✓ Python deps installed"

# ── Step 3: Bootstrap Terraform state bucket + lock table ──────────────

step "3/6 — Bootstrap Terraform backend (state bucket + lock table)"
TF_STATE_BUCKET="${PREFIX}-tf-state-${ACCOUNT_ID}-${REGION}"
LOCK_TABLE="${PREFIX}_tf_lock"

if ! aws s3api head-bucket --bucket "$TF_STATE_BUCKET" --region "$REGION" 2>/dev/null; then
  info "  Creating state bucket: $TF_STATE_BUCKET"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$TF_STATE_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$TF_STATE_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-bucket-versioning --bucket "$TF_STATE_BUCKET" --region "$REGION" \
    --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "$TF_STATE_BUCKET" --region "$REGION" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "$TF_STATE_BUCKET" --region "$REGION" \
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  info "  ✓ State bucket created"
else
  info "  ✓ State bucket exists"
fi

if ! aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$REGION" >/dev/null 2>&1; then
  info "  Creating lock table: $LOCK_TABLE"
  aws dynamodb create-table --table-name "$LOCK_TABLE" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null
  aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$REGION"
  info "  ✓ Lock table created"
else
  info "  ✓ Lock table exists"
fi

# ── Step 4: Terraform — provision all AWS resources ────────────────────

step "4/6 — Terraform: provisioning all AWS resources"
TF_VARS=(-var="project_prefix=${PREFIX}" -var="aws_region=${REGION}")
[[ -n "${BEDROCK_MODEL_ID:-}" ]] && TF_VARS+=(-var="bedrock_model_id=${BEDROCK_MODEL_ID}")

# Public hosting + Cognito (UAT / release).
if [[ "$ENABLE_PUBLIC_HOSTING" == "true" ]]; then
  [[ -n "$PORTAL_DOMAIN_NAME" && -n "$PORTAL_ACM_CERTIFICATE_ARN" ]] || {
    error "ENABLE_PUBLIC_HOSTING=true needs PORTAL_DOMAIN_NAME and PORTAL_ACM_CERTIFICATE_ARN (us-east-1 ACM certificate)"; exit 1; }
  TF_VARS+=(-var="enable_public_hosting=true" -var="portal_domain_name=${PORTAL_DOMAIN_NAME}"
    -var="portal_acm_certificate_arn=${PORTAL_ACM_CERTIFICATE_ARN}")
fi
if [[ "$ENABLE_COGNITO" == "true" ]]; then
  TF_VARS+=(-var="enable_cognito=true" -var="admin_email=${ADMIN_EMAIL}")
fi
TF_VARS+=(-var="graph_backend=${GRAPH_BACKEND}" -var="enable_deploy=${ENABLE_DEPLOY}")
if [[ -n "$CLOUDFRONT_ALLOWED_CIDRS" ]]; then
  CIDRS_HCL="[$(echo "$CLOUDFRONT_ALLOWED_CIDRS" | tr -d ' ' | sed 's/[^,][^,]*/"&"/g')]"
  TF_VARS+=(-var="cloudfront_allowed_cidrs=${CIDRS_HCL}")
fi

(
  cd "$TF_DIR"
  terraform init -reconfigure -input=false \
    -backend-config="bucket=${TF_STATE_BUCKET}" \
    -backend-config="dynamodb_table=${LOCK_TABLE}" \
    -backend-config="region=${REGION}" 2>&1 | tail -3
  info "  ✓ Terraform initialized"
  terraform apply -auto-approve -input=false "${TF_VARS[@]}" 2>&1 | tail -12
  info "  ✓ Terraform applied — all AWS resources provisioned"
)

# Collect outputs
tf_out() { (cd "$TF_DIR" && terraform output -raw "$1" 2>/dev/null || echo ""); }
DDB_TABLE="$(tf_out dynamodb_table_name)"
DATA_BUCKET="$(tf_out s3_bucket)"
WORKER_REPO_URL="$(tf_out ecr_repository_url)"
GATEWAY_ID="$(tf_out agentcore_gateway_id)"
GATEWAY_URL="$(tf_out agentcore_gateway_endpoint)"
HARNESS_ARN="$(tf_out harness_arn)"
SFN_NAME="$(tf_out sfn_state_machine_name)"
PORTAL_URL="$(tf_out portal_url)"
SITE_BUCKET="$(tf_out site_bucket_name)"
CF_DIST_ID="$(tf_out cloudfront_distribution_id)"
COGNITO_POOL_ID="$(tf_out cognito_user_pool_id)"
COGNITO_CLIENT_ID="$(tf_out cognito_app_client_id)"
COGNITO_DOMAIN="$(tf_out cognito_hosted_ui_domain)"

# Sync the standardized template library (Terraform modules + CI/CD) to the data
# bucket so the provision_infrastructure / generate_cicd tools can render from it.
if [[ -n "$DATA_BUCKET" ]]; then
  info "  Syncing template library → s3://${DATA_BUCKET}/config/templates/"
  if aws s3 sync backend/resources/templates/ "s3://${DATA_BUCKET}/config/templates/" --region "$REGION" --delete --quiet; then
    info "  ✓ Templates synced"
  else
    warn "  Template sync failed (provision_infrastructure/generate_cicd will error until fixed)"
  fi
fi

# ── Step 5: Build + push the ATX worker image ──────────────────────────

step "5/6 — Docker: build + push ATX worker image to ECR"
if [[ -z "$WORKER_REPO_URL" ]]; then
  ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  WORKER_REPO_URL="${ECR_BASE}/${PREFIX}-worker"
else
  ECR_BASE="${WORKER_REPO_URL%/*}"
fi
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

info "  Building image (linux/amd64)..."
docker build --platform linux/amd64 \
  -t "${PREFIX}-worker:latest" -f docker/Dockerfile.worker . 2>&1 | tail -3
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_BASE" 2>&1 | grep -i "login succeeded" || true
docker tag "${PREFIX}-worker:latest" "${WORKER_REPO_URL}:latest"
docker tag "${PREFIX}-worker:latest" "${WORKER_REPO_URL}:${TIMESTAMP}"
docker push "${WORKER_REPO_URL}:latest" 2>&1 | tail -1
docker push "${WORKER_REPO_URL}:${TIMESTAMP}" 2>&1 | tail -1
info "  ✓ Pushed ${WORKER_REPO_URL}:latest (+ :${TIMESTAMP})"

# The API service runs the SAME :latest worker image; ECS will not pull a fresh
# :latest on its own, so force a rolling redeploy or the API keeps serving stale
# code (e.g. new routes 405 until the next task cycle).
if [[ "$ENABLE_PUBLIC_HOSTING" == "true" ]]; then
  if aws ecs describe-services --cluster "${PREFIX}-cluster" --services "${PREFIX}-api-service" \
       --region "$REGION" --query "services[0].status" --output text 2>/dev/null | grep -q ACTIVE; then
    aws ecs update-service --cluster "${PREFIX}-cluster" --service "${PREFIX}-api-service" \
      --force-new-deployment --region "$REGION" --query 'service.serviceName' --output text >/dev/null \
      && info "  ✓ API service rolling onto new image (${PREFIX}-api-service)"
  fi
fi

# ── Step 5b: Build + deploy the frontend to CloudFront (UAT only) ──────

if [[ "$ENABLE_PUBLIC_HOSTING" == "true" && -n "$SITE_BUCKET" ]]; then
  step "5b/6 — Frontend: build + deploy to CloudFront"
  # Build-time config the SPA reads (Vite loads .env.production.local automatically).
  cat > frontend/.env.production.local <<EOF
VITE_API_BASE_URL=/api/v1
VITE_DEV_AUTH=false
VITE_COGNITO_DOMAIN=${COGNITO_DOMAIN}
VITE_COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID}
VITE_REDIRECT_URI=${PORTAL_URL}
EOF
  (
    cd frontend
    # pnpm-lock.yaml is the maintained lockfile (package-lock.json is stale),
    # and node_modules is pnpm-managed, which npm cannot read. Same install as CI.
    # pipefail (set above) aborts the deploy on a failed install/build, so a broken bundle
    # is never synced to the site bucket with --delete.
    pnpm install --frozen-lockfile 2>&1 | tail -8
    pnpm run build 2>&1 | tail -8
  )
  aws s3 sync frontend/dist "s3://${SITE_BUCKET}" --region "$REGION" --delete --quiet
  info "  ✓ UI synced → s3://${SITE_BUCKET}"
  if [[ -n "$CF_DIST_ID" ]]; then
    aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" --paths "/*" \
      --query 'Invalidation.Id' --output text >/dev/null && info "  ✓ CloudFront invalidated"
  fi
fi

# ── Step 6: Sync .env with Terraform outputs ───────────────────────────

step "6/6 — Writing resolved config to .env"
set_env .env PROJECT_PREFIX "${PREFIX}"
set_env .env AWS_REGION "${REGION}"
set_env .env DYNAMODB_TABLE "${DDB_TABLE:-${PREFIX}_portfolio}"
set_env .env S3_BUCKET "${DATA_BUCKET:-${PREFIX}-data-${ACCOUNT_ID}-${REGION}}"
set_env .env SFN_STATE_MACHINE_NAME "${SFN_NAME:-${PREFIX}-agentcore-workflow}"
set_env .env AGENTCORE_INVOKE_LAMBDA "${PREFIX}-invoke-runtime"
set_env .env AGENTCORE_GATEWAY_ID "${GATEWAY_ID}"
set_env .env AGENTCORE_GATEWAY_URL "${GATEWAY_URL}"
[[ -n "${HARNESS_ARN}" ]] && set_env .env AGENTCORE_HARNESS_ARN "${HARNESS_ARN}"
# .env drives LOCAL dev (scripts/start.sh). Neptune sits in private subnets the laptop
# can't reach, so local dev stays on the DynamoDB-derived graph; the hosted API gets the
# chosen backend from Terraform. none carries over.
case "$GRAPH_BACKEND" in
  neptune) set_env .env GRAPH_BACKEND "dynamodb" ;;
  *)       set_env .env GRAPH_BACKEND "$GRAPH_BACKEND" ;;
esac

# Auth: with Cognito provisioned, the API enforces JWTs (DEV_AUTH off); otherwise
# local dev keeps the dev bypass.
if [[ "$ENABLE_COGNITO" == "true" && -n "$COGNITO_POOL_ID" ]]; then
  set_env .env DEV_AUTH "false"
  set_env .env COGNITO_USER_POOL_ID "${COGNITO_POOL_ID}"
  set_env .env COGNITO_APP_CLIENT_ID "${COGNITO_CLIENT_ID}"
else
  set_env .env DEV_AUTH "true"
fi
info "  ✓ .env updated"

# ── Summary ────────────────────────────────────────────────────────────

echo ""
info "╔══════════════════════════════════════════════════════════════╗"
info "║  Setup Complete                                              ║"
info "╠══════════════════════════════════════════════════════════════╣"
info "║  Account:      ${ACCOUNT_ID}"
info "║  Region:       ${REGION}"
info "║  Prefix:       ${PREFIX}"
info "║  State:        s3://${TF_STATE_BUCKET}"
info "║  DynamoDB:     ${DDB_TABLE}"
info "║  Data bucket:  ${DATA_BUCKET}"
info "║  Worker image: ${WORKER_REPO_URL}:latest"
info "║  Gateway:      ${GATEWAY_ID}"
info "║  Workflow:     ${SFN_NAME}"
[[ -n "$PORTAL_URL" ]] && info "║  Portal:       ${PORTAL_URL}"
[[ -n "$PORTAL_URL" ]] && info "║  DNS:          ${PORTAL_DOMAIN_NAME} → $(tf_out portal_cloudfront_domain_name)"
info "╚══════════════════════════════════════════════════════════════╝"
echo ""

if [[ "$ENABLE_COGNITO" == "true" && -n "$COGNITO_POOL_ID" ]]; then
  # The temporary password is never echoed (CI/terminal logs). Read it once, then sign in and reset it.
  warn "Cognito admin (reset required on first login):"
  warn "  user: ${ADMIN_EMAIL}"
  warn "  temp password: (cd ${TF_DIR} && terraform output -raw cognito_admin_temp_password)"
fi

if [[ -n "$PORTAL_URL" ]]; then
  info "Open the portal:   ${PORTAL_URL}"
else
  info "Start a workflow:  curl -X POST http://localhost:8000/api/v1/workflow/start -d '{\"app_id\":\"APP-XXX\"}'"
  info "Run the API:       scripts/start.sh"
fi
