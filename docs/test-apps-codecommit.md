# Test apps: CodeCommit repos and their 7R targets

A reference for end-to-end testing in a sandbox environment (for example account `111122223333`, `us-east-1`). It records
which CodeCommit repo and app exercises which strategy, compute, database and CI/CD path. **Create your own CodeCommit
repositories** (import the public samples listed below into your account) and link them to the apps; the repo names
here are examples.

- **Catalogue:** [`scripts/test_apps.json`](../scripts/test_apps.json), with 13 apps covering all 7 Rs. Register or
  refresh them with:
  ```
  backend/.venv/bin/python scripts/e2e_golden.py --apps-file scripts/test_apps.json --apps all --register-only
  ```
  Run one journey with `--apps-file scripts/test_apps.json --apps APP-MUSIC`. This needs a local API with
  `DEV_AUTH=true` (and Cognito unset in that shell), or `--token "$JWT"` against the portal.
- **Branch `atx-run7`:** in every repo this is the **original upstream code, before any AMF/ATX change**. Every app
  is linked to that branch, and every run starts from it. Each app's `main` may already contain earlier AMF/ATX output.
- **Registered:** 2026-11-04.

AMF scope:

| Strategy | Compute | Database | CI/CD |
|---|---|---|---|
| rehost / relocate (lift) | `ec2` | `same_as_source`, `dynamodb`, `rds_mysql` | `codepipeline` |
| replatform / refactor | `ecs_fargate`, `lambda` | `dynamodb`, `rds_postgresql`, `rds_mysql`, `aurora_postgresql`, `aurora_mysql`, `none` | `codepipeline` |
| retire / retain / repurchase (close-out) | none | none | none |

Clone URL pattern: `https://git-codecommit.us-east-1.amazonaws.com/v1/repos/<repo>`.

## Summary

| App | Repo | `build_dir` | 7R | Compute | Database | CI/CD | `atx-run7` = |
|---|---|---|---|---|---|---|---|
| **APP-TE** PetClinic | `amazon-ecs-java-microservices` | `1_ECS_Java_Spring_PetClinic` | replatform | `ecs_fargate` | `rds_mysql` | `codepipeline` | `cb56713` (upstream 2018-01-04) |
| **APP-HE** Unicorn Store | `java-on-aws` | `apps/unicorn-store-spring` | replatform | `ecs_fargate` | `rds_postgresql` | `codepipeline` | `6b241c31` (upstream 2026-07-29) |
| **APP-CLI** Product Catalog API | `serverless-java-frameworks-samples` | `springboot` | replatform | `lambda` | `dynamodb` | `codepipeline` | `1352ab4` (upstream 2024-01-10) |
| **APP-MUSIC** Music API v2 | `deploy-python-flask-microservices-to-aws-using-open-source-tools-part2` | root | replatform | `ecs_fargate` | `dynamodb` | `codepipeline` | `93eda63` (upstream 2022-10-12) |
| **APP-MUSIC-V1** Music API v1 | `deploy-python-flask-microservices-to-aws-using-open-source-tools` ⁺ | `app` | replatform | `ecs_fargate` | `dynamodb` | `codepipeline` | `2743d0b` (aws-samples `main`) |
| **APP-UNISHOP** Unishop monolith | `unishop-monolith-to-microservices` | `MonoToMicroLegacy` | refactor (stage 1) | `ecs_fargate` | `rds_mysql` | `codepipeline` | `d2a195b` (aws-samples import) |
| **APP-UNISHOP-SVC** Unishop services | `unishop-monolith-to-microservices` | `MonoToMicroLambda` | refactor (stage 2) | `lambda` | `dynamodb` | `codepipeline` | `d2a195b` |
| **APP-WEBAPP** Java web app (WAR) | `replatform-java-webapp-on-aws` | `sample-webapp` | replatform | `ecs_fargate` | `rds_mysql` | `codepipeline` | `57cbf19` (upstream 2021-11-26) |
| **APP-PETCLINIC-EC2** PetClinic | `aws-ecs-cicd-terraform` ⁺ | `petclinic` | **rehost** | `ec2` (`ami_id=al2023`) | `rds_mysql` | `codepipeline` | `dece821` (aws-samples `master`) |
| **APP-PETCLINIC-RELO** PetClinic | `aws-apprunner-terraform` ⁺ | `petclinic` | **relocate** | `ec2` (`ami_id=al2023`) | `same_as_source` | `codepipeline` | `f000687` (aws-samples `master`) |
| **APP-CDK** CDK sample catalogue | `aws-cdk-examples` | – | **retire** | – | – | – | `6408f42` (upstream 2026-09-22) |
| **APP-CARDDEMO** CardDemo mainframe | `aws-mainframe-modernization-carddemo` ⁺ | – | **retain** | – | – | – | `59cc6c2` (aws-samples `main`) |
| **APP-WORDPRESS** Corporate site | `aws-refarch-wordpress` ⁺ | – | **repurchase** | – | – | – | `ad192b9` (aws-samples `master`) |

⁺ Imported from the public aws-samples `<repo>` repository into CodeCommit on 2026-11-04 (`main` and `atx-run7` both point at
the upstream HEAD; tags `Project=amf, Purpose=test-seed`).

Coverage:
- **Strategy:** replatform ×6, refactor ×2, rehost, relocate, retire, retain, repurchase.
- **Compute:** ECS ×6, Lambda ×2, EC2 ×2.
- **Database:** RDS MySQL ×4, RDS PostgreSQL ×1, DynamoDB ×4, same-as-source ×1.

## Per app

### APP-TE: PetClinic (`amazon-ecs-java-microservices`)
- **Stack:** Spring Boot **1.5.2**, Java **8**, Maven, MySQL, Dockerfile in `src/main/docker`.
- **Target:** replatform → ECS Fargate + RDS MySQL.
- **Expected ATX:** Java 8→21 and a Spring Boot upgrade.
- **Exercises:** an old-Java upgrade, RDS MySQL wiring (Spring datasource + RDS-managed secret), and the ECS deploy.
- **Note:** `2_…Microservices` / `3_…CICD` hold a 5-service variant. AMF supports one compute per app.

### APP-HE: Unicorn Store (`java-on-aws`)
- **Stack:** Spring Boot (JPA, WebMVC/WebFlux, actuator), Java 21, Maven, **PostgreSQL**, EventBridge, Dockerfile.
- **Target:** replatform → ECS Fargate + RDS PostgreSQL.
- **Exercises:** RDS PostgreSQL wiring and the actuator health path (repo scan).

### APP-CLI: Product Catalog API (`serverless-java-frameworks-samples`)
- **Stack:** SAM `springboot/template.yaml`, 4 Java **17** functions (Spring Cloud Function, 1024 MB), API Gateway
  `/products`, `/products/{id}`, DynamoDB.
- **Target:** replatform → Lambda + DynamoDB.
- **Expected ATX:** Java 17→21.
- **Exercises:** SAM → Terraform (multi-function), the Lambda CodeBuild deploy, and per-function checks.

### APP-MUSIC / APP-MUSIC-V1: Flask music API (parts 2 and 1)
- **Stack:** Flask, **Python 3.8** (end of life), Dockerfile, DynamoDB `musicTable` (boto3). Both repos ship their
  own Terraform. Part 2 also has ECS, ECR and CodeDeploy, `buildspec.yml` and `appspec.yaml`.
- **Target:** replatform → ECS Fargate + DynamoDB. Part 1's app is in `app/`; part 2's is at the repo root.
- **Expected ATX:** Python 3.8→3.12.
- **Exercises:** a Python app, DynamoDB wiring from the repo scan, and replacing in-repo IaC with AMF templates.

### APP-UNISHOP / APP-UNISHOP-SVC (`unishop-monolith-to-microservices`)
- **Monolith:** `MonoToMicroLegacy`: Spring Boot, **Gradle** (`gradlew`), MySQL, Spring Security. Target: ECS
  Fargate + RDS MySQL. Exercises the Gradle build and the Gradle-wrapper path of the security scan.
- **Services:** `MonoToMicroLambda`: 5 Java functions (Gradle) + `MonoToMicroDynamoDB`. Target: Lambda + DynamoDB.

### APP-WEBAPP: Java web app (WAR) (`replatform-java-webapp-on-aws`)
- **Stack:** `sample-webapp` with `<packaging>war</packaging>`, Tomcat 9, MySQL; plus `aws-signup-lambda` (Cognito
  trigger, separate).
- **Target:** replatform → ECS Fargate + RDS MySQL.
- **⚠ Known gap:** AMF's generated Dockerfile and its EC2 module only handle **executable JARs**. Before testing,
  commit a Tomcat Dockerfile to `sample-webapp/` on `atx-run7` (e.g. a Tomcat 9 base image plus the WAR in
  `webapps/ROOT.war`); a repo Dockerfile is used as-is. Otherwise expect Execution to fail at the image or CI step.

### APP-PETCLINIC-EC2 (rehost) / APP-PETCLINIC-RELO (relocate)
- **Stack:** Spring PetClinic **executable JAR**, Spring Boot **2.3.3**, Java **8**, Maven, MySQL connector + H2.
- **Rehost:** EC2 (fresh AL2023 + Corretto + generated systemd unit) + RDS MySQL. The app gets the endpoint and the
  RDS secret at boot, and the matching Spring profile is activated.
- **Relocate:** EC2 with `same_as_source`; the app keeps its embedded H2 / existing DB.
- **Exercises:** the lift path (no ATX transforms; strategy gating zeroes them) and the EC2 module + CodeDeploy revision.

### APP-CDK (retire) / APP-CARDDEMO (retain) / APP-WORDPRESS (repurchase)
- **Close-out path:** Discovery → `Handover_CloseOut` → `Validate_CloseOut` → `handover_review` → completed. There is
  no Assessment, Design or Execution.
- **Exercises:** the close-out document checks (`tool_documents._check_closeout_document`: machine count, owners,
  RPO/RTO, dependencies, cost, risk score, deadlines).

## Before running any test
1. **Fresh runs only.** All apps start a new run, which resets run-derived fields. Infra generated before 2026-11-04
   lacks the **app permissions boundary** and can't be deployed. A new run regenerates it.
2. **Credentials.** CodeCommit uses the task role; no Git token exists.
3. **Roles.** Approving `execution_review` (the deploy) needs the **Admin** group. The other gates need Engineer.
4. **Validation smoke.** An `amf-smoke.json` can only check the app's own table; `verify.table` is ignored
  . The original code on `atx-run7` has none, so L2/L3 run their defaults.

## Expected checks per test

| Check | Where it shows up |
|---|---|
| Strategy confidence and signals | Discovery gate (`discovery_review`) |
| ATX transforms selected and run in order (replatform/refactor) | Assessment gate checklist; Execution plan steps |
| Infra for the approved compute only; `terraform plan` clean | Execution (`exec_infra_status`, plan summary) |
| CI readiness / CI fix | Execution notes (`ANALYSIS#ci-readiness`) |
| Deploy of the pinned commit + first pipeline run | `execution_review` → Deploy → `Check_Deploy` |
| L0 platform, L1 smoke, L2 contract, L3 data | Validation screen (run history, per-case evidence) |
| Close-out document with no contradictions | `handover_review` gate (close-out apps) |
