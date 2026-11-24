# Troubleshooting

Common issues and their solutions.

## Workflow Issues

### Run goes straight to `MarkFailed` at a `PreHook_*` state

**Cause:** A deterministic pre-hook (`invoke_runtime` `mode=prehook_phase`) found a missing input.
It fails fast rather than letting the agent run under-fed.

**Fix:** Read the `blockers[]` in the failed state's output (Step Functions console, or
`GET /api/v1/workflow/execution/{app_id}`). Typical blockers:
- Discovery: no `app_name`, or no linked repo (`REPO#`).
- Assessment: strategy unresolved, or `repo_url_invalid:*` (a bare repo name instead of a clonable URL).
- Design: no `assessment-summary` from the **current** run.
- Execution: `target_compute` / `target_database` / `cicd_target` not valid for the strategy,
  `target_account` not 12 digits, or `vpc_cidr` not RFC1918.

### Assessment or Design fails after one re-run

**Cause:** The post-check (`mode=validate_phase`) found contradictions. Examples: an assessment
written for a different strategy, or a design that contradicts the approved compute, DB, CI/CD,
account or CIDR. The phase re-runs once with the blockers in its prompt, then goes to `MarkFailed`.

**Fix:** Check the `consistency_contradictions` in the stored analysis or design. Correct the app's
recorded facts if they are wrong, then start a new run.

### `AccessDeniedException` at the first agent phase

**Cause:** The SFN role is missing `bedrock-agentcore:InvokeAgentRuntime` (`InvokeHarness` alone is
not enough), or the Harness runtime role is missing the AgentCore Memory actions (`ListEvents`, …).

**Fix:** Re-apply Terraform (`step-functions.tf`, `main.tf`). Both permissions are defined there.

### Workflow stuck at `LaunchTransformation` for hours

**Cause:** The app has no `transformation_steps`. `start_code_transformation` then does nothing,
and the `waitForTaskToken` waits until the 4 h timeout.

**Fix:** Seed the steps with `scripts/seed_transformation_steps.py --app-id <APP> --steps <json> --table <prefix>_portfolio`.

### UI shows "Running" but nothing happens / wrong state machine

**Cause:** `SFN_STATE_MACHINE_NAME` in `.env` doesn't match the deployed `{prefix}-agentcore-workflow`,
or `PROJECT_PREFIX` is wrong.

**Fix:** Re-run `scripts/setup.sh <prefix>`, which rewrites `.env` from the Terraform outputs.
Check with `GET /api/v1/system/capabilities` (`sfn_state_machine_name`). `current_step` only shows a
`*_review` value while an approval gate is open.

### `/health/ready` returns 503

The `code` field in the response says which:
- `AWS_CREDENTIALS_EXPIRED`: refresh your local AWS credentials.
- `AWS_DEPENDENCY_UNAVAILABLE`: the table or state machine for the configured prefix doesn't exist.

### Design documents / reports 500 with `NoSuchBucket`

**Cause:** `S3_BUCKET` is unset and the derived name fell back to an account-less value (STS was
unavailable at startup).

**Fix:** Set `S3_BUCKET` explicitly. `setup.sh` writes it, and the hosted API task sets it.

### Wave Planning page missing

**Cause:** `GRAPH_BACKEND=none`. Wave planning is on for `dynamodb` (default) and `neptune`.

**Fix:** Set `GRAPH_BACKEND=dynamodb` in `.env` and restart the backend.

---

## ECS / ATX Issues

### ATX "Permission denied"

**Cause:** ATX binary not accessible to the container user.

**Fix:** Rebuild the Docker image:
```bash
docker build --platform linux/amd64 -t ${PREFIX}-worker:latest -f docker/Dockerfile.worker .
```

Verify:
```bash
docker run --rm --entrypoint bash ${PREFIX}-worker:latest -c "whoami && which atx"
# Expected: atxuser, /usr/local/bin/atx
```

### ECS tasks stay in PENDING

**Possible causes:**
1. **No internet access** — subnet needs public IP + internet gateway, or NAT gateway
2. **ECR pull fails** — check task execution role has `ecr:GetAuthorizationToken` and `ecr:BatchGetImage`
3. **Subnet capacity** — try a different subnet
4. **Task definition not found** — verify `ASSESSMENT_ECS_TASK_DEFINITION` (default `{prefix}-worker-task`) matches what's deployed

**Debug:**
```bash
aws ecs describe-tasks --cluster ${PREFIX}-cluster --tasks <task-arn>
```

Look at `stoppedReason` and `containers[].reason`.

### ATX run ID not discovered

**Cause:** ATX didn't create the expected directory structure.

**Check ECS logs for:**
```
[ATX] ERROR: Could not discover ATX run ID
[ATX] DEBUG: ATX_CUSTOM_DIR=...
```

ATX creates run directories at `~/.aws/atx/custom/<run_id>/`. If the directory doesn't exist, ATX may have failed silently.

### ATX clone fails

**Cause:** AMF clones AWS CodeCommit only, with the worker task role. A non-CodeCommit URL is refused
(`repo_url_invalid` at the Assessment pre-hook), and a missing repo or a task role without
`codecommit:GitPull` on it fails the clone.

**Fix:** Mirror the application repo into CodeCommit, link it as
`https://git-codecommit.<region>.amazonaws.com/v1/repos/<name>`, then start a new run. The ATX worker
fails the callback when linked repos analyse nothing (clone failure or 0 files), so this shows up as a
failed `LaunchATX`.

### ATX output not where expected

**Expected:** `s3://{prefix}-data-{account}-{region}/applications/{app_id}/analysis/{td_prefix}/{atx_run_id}/`

**Cause:** Old worker image. Re-run `scripts/setup.sh <prefix>` to rebuild and push it.

---

## Bedrock Issues

### "Invocation of model ID ... with on-demand throughput isn't supported"

**Cause:** Using a model ID that requires cross-region inference profile.

**Fix:** Use `us.anthropic.claude-sonnet-4-6` (with `us.` prefix) in `BEDROCK_MODEL_ID`.

### "ValidationException: ... not authorized to perform transform-custom:ConverseStream"

**Cause:** The worker task role is missing the AWS Transform custom or Bedrock grants that
`infra/modules/worker-task/main.tf` defines. Re-apply Terraform rather than editing the role by hand.

**Reference:** the grants the module sets (replace the account and Region with yours):
```json
[
  {
    "Sid": "TransformCustomCli",
    "Effect": "Allow",
    "Action": ["transform-custom:*"],
    "Resource": "*"
  },
  {
    "Sid": "TransformCustomNoShareOrDelete",
    "Effect": "Deny",
    "Action": ["transform-custom:PutResourcePolicy", "transform-custom:DeleteResourcePolicy",
               "transform-custom:ShareTransformationPackage", "transform-custom:UnshareTransformationPackage",
               "transform-custom:Delete*"],
    "Resource": "*"
  },
  {
    "Sid": "BedrockModel",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:us-east-1:111122223333:inference-profile/*"
    ]
  }
]
```
`transform-custom` keeps `"*"`: the CLI calls actions that have no resource type (for example
`ConverseStream` and `ListTransformationPackageMetadata`), and AWS-managed transformation definitions are
owned by AWS, not by your account. The Bedrock `Converse` APIs are authorized by `bedrock:InvokeModel*`; the
foundation-model ARN uses `*` for the Region because a `us.` inference profile routes across US Regions.

---

## Frontend Issues

### Blank screen on port 3000

**Check:**
1. Is the frontend running? `bash scripts/start.sh frontend`
2. Browser console for errors
3. Vite config — ensure proxy to backend is correct

### CORS errors

**Cause:** Backend not allowing frontend origin.

**Fix:** Add the origin to `CORS_ORIGINS` (comma-separated, default `http://localhost:3000`) in `.env`.

---

## Database Issues

### "Table does not exist"

**Fix:** Check `PROJECT_PREFIX` / `DYNAMODB_TABLE` in `.env`, or provision with:
```bash
scripts/setup.sh <prefix>
```

Or create manually (replace `{prefix}` with your PROJECT_PREFIX, default `amf`):
```bash
aws dynamodb create-table \
  --table-name ${PREFIX}_portfolio \
  --attribute-definitions \
    AttributeName=PK,AttributeType=S \
    AttributeName=SK,AttributeType=S \
    AttributeName=GSI1PK,AttributeType=S \
    AttributeName=GSI1SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --global-secondary-indexes '[{"IndexName":"GSI1","KeySchema":[{"AttributeName":"GSI1PK","KeyType":"HASH"},{"AttributeName":"GSI1SK","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]' \
  --billing-mode PAY_PER_REQUEST
```

### Float/Decimal errors

**Cause:** DynamoDB doesn't support Python `float`. The code converts to `Decimal` automatically via `db/helpers.to_dynamo()`.

If you see this error, a float slipped through — check the data being written.

---

## General

### Port already in use

```bash
# Kill processes on ports 8000 and 3000
lsof -ti:8000 | xargs kill -9
lsof -ti:3000 | xargs kill -9
```

Or use `scripts/start.sh` which handles this automatically.

### Python import errors

Ensure you're using the venv:
```bash
source backend/.venv/bin/activate
```

Or let `scripts/start.sh` handle it.
