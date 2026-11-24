# CI/CD Setup

AMF's own pipeline is **CI only**: validate, test and security-scan. **CD is disabled** (see the `.gitlab-ci.yml` header, "CD disabled until UAT environment is ready"). Deploys run from a workstation with `scripts/setup.sh [prefix]`; see [deployment.md](deployment.md).

> This page covers the pipeline that builds AMF itself. The CI/CD pipelines AMF *generates* for migrated apps are **AWS CodePipeline only** (CodeCommit source → CodeBuild → ECS deploy / CodeDeploy / Lambda update). They come from `backend/resources/templates/codepipeline/` plus the Terraform `codepipeline` module, and are committed by the `generate_cicd` tool. They are separate.

## Pipeline (`.gitlab-ci.yml`)

The pipeline runs for merge requests, the default branch, `dev` and `feature/*` branches.

Every job is blocking.

| Stage | Job | What it does |
|---|---|---|
| validate | `validate:python` | `ruff check backend/src backend/tests` (ruff pinned) |
| validate | `validate:typescript` | `pnpm install --frozen-lockfile` then `pnpm exec tsc --noEmit` in `frontend/` |
| validate | `validate:terraform` | `terraform fmt -check -recursive infra`, `init -backend=false`, `validate` |
| test | `test:unit` | backend `pytest backend/tests/unit/` + JUnit |
| test | `test:lambda` | tool-Lambda `pytest` in `infra/environments/agentcore/lambda-src` + JUnit |
| test | `test:frontend` | `pnpm test` (vitest) |
| security | `ash-sast` | [ASH](https://github.com/awslabs/automated-security-helper) v3.7.0 with `ash_config.yaml`. MRs: changed files against the merge base; `dev` / `feature/*`: full scan. Publishes JUnit, CycloneDX and GitLab SAST reports. |
| upload | `upload-security-report` | Copies `.ash/ash_output/` to `s3://${SECURITY_REPORTS_BUCKET}/…` (`dev` / `feature/*`; non-blocking if the role trust is missing) |

### Variables (`.gitlab/ci/variables.yml`)

This is the only file included from `.gitlab/ci/`. It sets:
- `AWS_REGION` / `AWS_DEFAULT_REGION` (`us-east-1`)
- `AWS_ROLE_ARN`: the CI role that vends credentials for `upload-security-report` through `AWS_CREDS_TARGET_ROLE`
- `AWS_ACCOUNT_ID`, `SECURITY_REPORTS_BUCKET`, `TF_VERSION`, `APP_PREFIX`
- empty mandatory tag variables (`CORRELATION_ID`, `CREATED_BY`, `CONTACT_DL`)

The file still has values inherited from another project (`ENVIRONMENT: nprd`, the `UNO Unified Orchestrator` header, and an account-specific role and bucket). Check them before pointing CI at a new account.

The old `.gitlab/ci/{backend-deploy,data,destroy,frontend,security,terraform,verify}.yml` jobs were never included. They referenced paths that don't exist, so they were removed.

## Security scanning (`ash_config.yaml`)

- `severity_threshold: MEDIUM`. Findings fail the job.
- **Ignored paths:** virtualenv, `node_modules`, bytecode, `.git`, `input/**` (synthetic discovery data).
- **Templates are scanned:** the generated-template tree `backend/resources/templates/**` is not excluded, because customers deploy it as is.
- **Time-boxed suppressions** cover the hosted demo front door (`public-hosting.tf`): ALB HTTPS/TLS/WAF, the site-bucket KMS/logging/versioning, WAF managed rules/logging, and the custom domain/ACM. They were re-dated on 2026-11-03 to **expire 2026-12-03** (project owner) as a stopgap until a later hardening phase lands. Suppressions are pinned to line numbers in `public-hosting.tf`, so re-pin them whenever that file changes.
- `project_name` is `amf`.

Run it locally:

```bash
pip install "git+https://github.com/awslabs/automated-security-helper.git@v3.7.0"
ash --config ash_config.yaml --output-dir .ash/ash_output
```

## Local equivalents (run before pushing)

```bash
backend/.venv/bin/ruff check backend/src backend/tests
backend/.venv/bin/python -m pytest backend/tests -q
(cd infra/environments/agentcore/lambda-src && ../../../../backend/.venv/bin/python -m pytest -q)
cd frontend && npx tsc --noEmit && npx vitest run
cd infra/environments/agentcore && terraform validate
```

## Known gaps

- Ruff and `tsc` don't block. Remove `|| true` and `allow_failure` once both are clean on `dev`.
- CI never runs the tool Lambda tests (`infra/environments/agentcore/lambda-src/test_*.py`).
- CI never runs the frontend tests (`npx vitest run`).
- CI has no `terraform validate` / `terraform fmt -check` job.
- No CD. A deploy job would need OIDC to a deploy role, then `scripts/setup.sh <prefix>` (Terraform apply, worker/API image push, template sync, API redeploy, SPA upload). It also needs a decision on Terraform state locking per environment.
