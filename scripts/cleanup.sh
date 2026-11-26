#!/usr/bin/env bash
set -euo pipefail
#
# Cleanup — two modes:
#   • default : wipes RUNTIME DATA (DynamoDB records + S3 objects) and stops any
#               running Step Functions executions / ECS tasks for one prefix.
#               Infrastructure is NOT touched.
#   • infra   : data reset AND `terraform destroy` of the ENTIRE deployment
#               (all AWS resources for the prefix). The TF state bucket + lock
#               table are bootstrapped outside Terraform and are left intact so
#               `scripts/setup.sh` can redeploy against the same backend.
#
# Destructive + irreversible. Requires explicit confirmation (or --yes).
#
# Usage:
#   scripts/cleanup.sh [--yes]           data reset (DynamoDB + S3 + SFN/ECS)
#   scripts/cleanup.sh infra [--yes]     data reset + terraform destroy (ALL resources)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step()  { echo -e "${CYAN}[STEP]${NC} $1"; }

cd "$PROJECT_ROOT"
# shellcheck source=/dev/null
[[ -f .env ]] && { set -a; source .env; set +a; }

# Args: optional "infra" + optional "--yes"
ASSUME_YES=0; DESTROY_INFRA=0
for a in "$@"; do
  case "$a" in
    infra|--infra|--all) DESTROY_INFRA=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
  esac
done

REGION="${AWS_REGION:-us-east-1}"
PREFIX="${PROJECT_PREFIX:-amf}"
# One STS call: arn:<partition>:sts::<account>:assumed-role/... → partition + account.
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "")
PARTITION=$(echo "$CALLER_ARN" | cut -d: -f2)
ACCOUNT_ID=$(echo "$CALLER_ARN" | cut -d: -f5)
[[ -z "$ACCOUNT_ID" || -z "$PARTITION" ]] && { error "Cannot determine AWS account. Check credentials."; exit 1; }

TABLE="${DYNAMODB_TABLE:-${PREFIX}_portfolio}"
BUCKET="${S3_BUCKET:-${PREFIX}-data-${ACCOUNT_ID}-${REGION}}"
SFN_NAME="${SFN_STATE_MACHINE_NAME:-${PREFIX}-agentcore-workflow}"
CLUSTER="${ATX_ECS_CLUSTER:-${PREFIX}-cluster}"
SFN_ARN="arn:${PARTITION}:states:${REGION}:${ACCOUNT_ID}:stateMachine:${SFN_NAME}"

echo ""
if [[ "$DESTROY_INFRA" == "1" ]]; then
warn "╔══════════════════════════════════════════════════════════════╗"
warn "║  FULL TEARDOWN — data reset + terraform destroy (irreversible) ║"
warn "╠══════════════════════════════════════════════════════════════╣"
warn "║  Account:   ${ACCOUNT_ID} (${REGION})"
warn "║  Prefix:    ${PREFIX}"
warn "║  Infra:     ALL Terraform-managed resources DESTROYED"
warn "║             (VPC, ECS, DynamoDB, S3, ECR, Lambdas, Gateway,"
warn "║              Harness, Step Functions, IAM, site bucket)"
warn "║  Kept:      TF state bucket + ${PREFIX}_tf_lock (backend, reused by setup)"
warn "╚══════════════════════════════════════════════════════════════╝"
else
warn "╔══════════════════════════════════════════════════════════════╗"
warn "║  DATA RESET — irreversible                                    ║"
warn "╠══════════════════════════════════════════════════════════════╣"
warn "║  Account:   ${ACCOUNT_ID} (${REGION})"
warn "║  DynamoDB:  ${TABLE}          (ALL records deleted)"
warn "║  S3:        ${BUCKET}         (ALL objects deleted)"
warn "║  SFN:       ${SFN_NAME}       (running executions stopped)"
warn "║  ECS:       ${CLUSTER}        (running tasks stopped)"
warn "╚══════════════════════════════════════════════════════════════╝"
fi
echo ""

if [[ "$ASSUME_YES" != "1" ]]; then
  PROMPT_ACTION="data wipe"; [[ "$DESTROY_INFRA" == "1" ]] && PROMPT_ACTION="FULL TEARDOWN (terraform destroy)"
  read -rp "Type the prefix '${PREFIX}' to confirm ${PROMPT_ACTION}: " CONFIRM
  [[ "$CONFIRM" == "$PREFIX" ]] || { error "Confirmation did not match. Aborted."; exit 1; }
fi

# ── 1. Stop running SFN executions ─────────────────────────────────────
step "1/4 — Stopping Step Functions executions"
RUNNING=$(aws stepfunctions list-executions --state-machine-arn "$SFN_ARN" --status-filter RUNNING \
  --region "$REGION" --query 'executions[*].executionArn' --output text 2>/dev/null || echo "")
if [[ -n "$RUNNING" && "$RUNNING" != "None" ]]; then
  n=0; for arn in $RUNNING; do aws stepfunctions stop-execution --execution-arn "$arn" --cause "cleanup" --region "$REGION" >/dev/null 2>&1 || true; n=$((n+1)); done
  info "  Stopped $n execution(s)"
else
  info "  No running executions"
fi

# ── 2. Stop running ECS tasks ──────────────────────────────────────────
step "2/4 — Stopping ECS tasks"
TASKS=$(aws ecs list-tasks --cluster "$CLUSTER" --region "$REGION" --query 'taskArns' --output text 2>/dev/null || echo "")
if [[ -n "$TASKS" && "$TASKS" != "None" ]]; then
  n=0; for t in $TASKS; do aws ecs stop-task --cluster "$CLUSTER" --task "$t" --reason "cleanup" --region "$REGION" >/dev/null 2>&1 || true; n=$((n+1)); done
  info "  Stopped $n task(s)"
else
  info "  No running tasks"
fi

# ── 3. Wipe DynamoDB ───────────────────────────────────────────────────
step "3/4 — Wiping DynamoDB ($TABLE)"
DYNAMODB_TABLE="$TABLE" AWS_REGION="$REGION" python3 - <<'PYEOF'
import boto3, os
t = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(os.environ["DYNAMODB_TABLE"])
deleted = 0; kw = {"ProjectionExpression": "PK, SK"}
while True:
    r = t.scan(**kw); items = r.get("Items", [])
    if items:
        with t.batch_writer() as b:
            for it in items:
                b.delete_item(Key={"PK": it["PK"], "SK": it["SK"]}); deleted += 1
    if "LastEvaluatedKey" not in r: break
    kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
print(f"  Deleted {deleted} records.")
PYEOF

# ── 4. Clear S3 objects ────────────────────────────────────────────────
step "4/4 — Clearing S3 objects (s3://$BUCKET)"
if aws s3 ls "s3://${BUCKET}/" --region "$REGION" >/dev/null 2>&1; then
  aws s3 rm "s3://${BUCKET}/" --recursive --region "$REGION" >/dev/null 2>&1 || true
  info "  Cleared current objects (versioned bucket — prior versions retained by lifecycle)"
else
  info "  Bucket not found / already empty"
fi

# ── Infra teardown: terraform destroy ──────────────────────────────────
if [[ "$DESTROY_INFRA" == "1" ]]; then
  TF_DIR="$PROJECT_ROOT/infra/environments/agentcore"
  TF_STATE_BUCKET="${PREFIX}-tf-state-${ACCOUNT_ID}-${REGION}"
  LOCK_TABLE="${PREFIX}_tf_lock"

  # The site bucket has force_destroy=false, so terraform destroy fails while it
  # holds objects. Empty it (current objects + all versions + delete markers)
  # first. Other buckets/ECR repos have force_destroy/force_delete=true.
  SITE_BUCKET="${PREFIX}-frontend-${ACCOUNT_ID}-${REGION}"
  step "infra 1/2 — Emptying site bucket ($SITE_BUCKET)"
  if aws s3api head-bucket --bucket "$SITE_BUCKET" --region "$REGION" 2>/dev/null; then
    SITE_BUCKET="$SITE_BUCKET" AWS_REGION="$REGION" python3 - <<'PYEOF'
import boto3, os
b = os.environ["SITE_BUCKET"]; s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
p = s3.get_paginator("list_object_versions"); n = 0
for page in p.paginate(Bucket=b):
    batch = [{"Key": o["Key"], "VersionId": o["VersionId"]}
             for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
    for i in range(0, len(batch), 1000):
        s3.delete_objects(Bucket=b, Delete={"Objects": batch[i:i+1000]}); n += len(batch[i:i+1000])
print(f"  Emptied {n} object version(s)")
PYEOF
  else
    info "  Site bucket not present — skipping"
  fi

  step "infra 2/2 — terraform destroy (prefix '${PREFIX}')"
  (
    cd "$TF_DIR"
    terraform init -reconfigure -input=false \
      -backend-config="bucket=${TF_STATE_BUCKET}" \
      -backend-config="dynamodb_table=${LOCK_TABLE}" \
      -backend-config="region=${REGION}" 2>&1 | tail -3
    TF_VARS=(-var="project_prefix=${PREFIX}" -var="aws_region=${REGION}")
    [[ -n "${BEDROCK_MODEL_ID:-}" ]] && TF_VARS+=(-var="bedrock_model_id=${BEDROCK_MODEL_ID}")
    terraform destroy -auto-approve -input=false "${TF_VARS[@]}" 2>&1 | tail -15
  )
  echo ""
  info "Teardown complete for prefix '${PREFIX}'. TF state bucket + ${PREFIX}_tf_lock kept."
  info "Redeploy with: ENABLE_PUBLIC_HOSTING=true ENABLE_COGNITO=true ADMIN_EMAIL=<you> ./scripts/setup.sh ${PREFIX}"
  exit 0
fi

echo ""
info "Data reset complete for prefix '${PREFIX}'. Infrastructure was not touched."
