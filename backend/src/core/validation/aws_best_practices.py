"""
AWS Best Practices Validation Engine.

Validates engineer inputs against AWS well-architected principles.
Returns validation result with pass/fail, warnings, and suggestions.
"""

from __future__ import annotations

from backend.src.config import settings


def validate_migration_inputs(app_data: dict, inputs: dict) -> dict:
    """Validate engineer inputs against AWS best practices.

    Args:
        app_data: Application metadata (tier, performance, compliance, etc.)
        inputs: Engineer selections from the migration plan form

    Returns:
        {
            "valid": bool,
            "errors": [{"field": str, "message": str, "suggestion": str}],
            "warnings": [{"field": str, "message": str, "suggestion": str}],
        }
    """
    errors = []
    warnings = []

    tier = app_data.get("tier", "tier_2")
    peak_tps = app_data.get("performance", {}).get("peak_tps", 0)
    uptime_sla = app_data.get("performance", {}).get("uptime_sla", "")
    data_volume = float(app_data.get("data_volume_gb", 0))
    db_engine = app_data.get("database", {}).get("engine", "").lower()
    compliance = app_data.get("compliance", [])

    compute = inputs.get("compute_service", "")
    target_db = inputs.get("target_database", "")
    multi_az = inputs.get("multi_az", "")
    region = inputs.get("target_region", "")
    deployment_strategy = inputs.get("deployment_strategy", "")
    backup_retention = int(inputs.get("backup_retention_days", 7))
    encryption = inputs.get("encryption", "")
    vpc = inputs.get("vpc", "")
    cicd = inputs.get("cicd_provider", "")

    # ── Compute Validation ─────────────────────────────────────────────

    if compute == "lambda" and data_volume > settings.bp_lambda_max_data_volume_gb:
        errors.append(
            {
                "field": "compute_service",
                "message": f"Lambda is not suitable for applications with {data_volume}GB data volume. Lambda has 15min timeout and limited storage.",
                "suggestion": "Use ECS Fargate or EKS for data-heavy applications.",
            }
        )

    if compute == "lambda" and peak_tps > settings.bp_lambda_max_peak_tps:
        warnings.append(
            {
                "field": "compute_service",
                "message": f"Lambda at {peak_tps} TPS may hit concurrency limits and incur high costs.",
                "suggestion": "Consider ECS Fargate for sustained high-throughput workloads.",
            }
        )

    if compute == "ec2" and tier == "tier_3":
        warnings.append(
            {
                "field": "compute_service",
                "message": "EC2 for Tier 3 apps adds operational overhead (patching, scaling).",
                "suggestion": "ECS Fargate or Lambda reduces operational burden for low-priority apps.",
            }
        )

    # ── High Availability ──────────────────────────────────────────────

    if multi_az == "no" and tier == "tier_1":
        errors.append(
            {
                "field": "multi_az",
                "message": "Single-AZ deployment for Tier 1 (critical) application violates HA policy.",
                "suggestion": "Enable Multi-AZ for production workloads with high availability requirements.",
            }
        )

    if multi_az == "no" and "99.9" in uptime_sla:
        errors.append(
            {
                "field": "multi_az",
                "message": f"Uptime SLA of {uptime_sla} requires Multi-AZ to meet availability target.",
                "suggestion": "Enable Multi-AZ. Single-AZ provides ~99.5% availability at best.",
            }
        )

    # ── Database Validation ────────────────────────────────────────────

    if target_db == "dynamodb" and db_engine in ("oracle", "postgresql", "postgres", "mysql", "sql server"):
        warnings.append(
            {
                "field": "target_database",
                "message": f"Migrating from relational DB ({db_engine}) to DynamoDB requires significant schema redesign.",
                "suggestion": f"Consider Aurora (PostgreSQL/MySQL compatible) for easier migration from {db_engine}.",
            }
        )

    if target_db == "documentdb" and db_engine not in ("mongodb", "documentdb"):
        errors.append(
            {
                "field": "target_database",
                "message": f"DocumentDB is MongoDB-compatible. Source is {db_engine} — not compatible.",
                "suggestion": "Use Aurora PostgreSQL or Aurora MySQL for relational source databases.",
            }
        )

    if data_volume > settings.bp_dynamodb_max_data_volume_gb and target_db == "dynamodb":
        warnings.append(
            {
                "field": "target_database",
                "message": f"DynamoDB for {data_volume}GB may be expensive. Review access patterns first.",
                "suggestion": "Consider Aurora Serverless v2 for large relational datasets, or S3 + Athena for analytics.",
            }
        )

    # ── Backup & DR ────────────────────────────────────────────────────

    if backup_retention < settings.bp_tier1_min_backup_days and tier == "tier_1":
        warnings.append(
            {
                "field": "backup_retention_days",
                "message": "7-day backup retention for Tier 1 app is below recommended minimum.",
                "suggestion": "Use 30+ days for critical applications. Consider cross-region backups.",
            }
        )

    if backup_retention < settings.bp_compliance_min_backup_days and compliance:
        errors.append(
            {
                "field": "backup_retention_days",
                "message": f"Compliance requirements ({', '.join(compliance)}) typically require 30+ day retention.",
                "suggestion": "Set backup retention to 35 days for SOX/PCI compliance.",
            }
        )

    # ── Deployment Strategy ────────────────────────────────────────────

    if deployment_strategy == "rolling" and tier == "tier_1":
        warnings.append(
            {
                "field": "deployment_strategy",
                "message": "Rolling deployment for Tier 1 apps risks partial outages during deploy.",
                "suggestion": "Use Blue/Green for zero-downtime deployments on critical applications.",
            }
        )

    # ── Region ─────────────────────────────────────────────────────────

    target_regions = inputs.get("target_regions", [])
    if not target_regions and not region:
        errors.append(
            {
                "field": "target_regions",
                "message": "At least one target region is required.",
                "suggestion": "Select a region close to your users. us-east-1 is default for US workloads.",
            }
        )

    if tier == "tier_1" and len(target_regions) < 2:
        warnings.append(
            {
                "field": "target_regions",
                "message": "Tier 1 apps should consider multi-region for DR.",
                "suggestion": "Add a secondary region for disaster recovery.",
            }
        )

    # ── Account ─────────────────────────────────────────────────────────

    if inputs.get("account") == "existing" and not inputs.get("account_id"):
        errors.append(
            {
                "field": "account_id",
                "message": "Selected 'Use existing account' but no Account ID provided.",
                "suggestion": "Provide the 12-digit AWS Account ID.",
            }
        )

    # ── Transformation Steps ───────────────────────────────────────────

    transform_steps = inputs.get("transformation_steps", [])
    if not transform_steps and compute not in ("ec2", "lambda"):
        warnings.append(
            {
                "field": "transformation_steps",
                "message": "No transformation steps defined. Code may need changes for target platform.",
                "suggestion": "Add the runtime/framework upgrades the assessment recommends.",
            }
        )

    # ── Testing ────────────────────────────────────────────────────────

    testing = inputs.get("testing_strategy", [])
    if not testing:
        warnings.append(
            {
                "field": "testing_strategy",
                "message": "No testing strategy selected.",
                "suggestion": "At minimum select 'unit', 'integration', and 'smoke' tests.",
            }
        )

    if tier == "tier_1" and "load" not in testing:
        warnings.append(
            {
                "field": "testing_strategy",
                "message": "Tier 1 app without load testing may miss performance regressions.",
                "suggestion": "Add 'load' testing to validate SLA under peak traffic.",
            }
        )

    if compliance and "security" not in testing:
        errors.append(
            {
                "field": "testing_strategy",
                "message": f"Compliance ({', '.join(compliance)}) requires security testing.",
                "suggestion": "Add 'security' to testing strategy.",
            }
        )

    # ── Encryption ─────────────────────────────────────────────────────

    if encryption == "none" and compliance:
        errors.append(
            {
                "field": "encryption",
                "message": f"No encryption selected but compliance requirements ({', '.join(compliance)}) mandate encryption.",
                "suggestion": "Use AWS Managed keys at minimum. CMK recommended for regulated workloads.",
            }
        )

    # ── VPC ────────────────────────────────────────────────────────────

    if vpc == "existing" and not inputs.get("vpc_id"):
        errors.append(
            {
                "field": "vpc",
                "message": "Selected 'Use existing VPC' but no VPC ID provided.",
                "suggestion": "Provide the VPC ID or select 'Create new'.",
            }
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": f"{len(errors)} error(s), {len(warnings)} warning(s)",
    }


# ── Default Suggestions Based on App Data ──────────────────────────────


def get_default_inputs(app_data: dict, strategy: str) -> dict:
    """Generate default input suggestions based on app metadata and strategy.

    These pre-fill the engineer form with intelligent defaults.
    """
    tier = app_data.get("tier", "tier_2")
    db = app_data.get("database", {})
    db_engine = db.get("engine", "").lower()
    perf = app_data.get("performance", {})
    peak_tps = perf.get("peak_tps", 0)
    language = app_data.get("language", "").lower()
    architecture = app_data.get("architecture", "monolith")
    compliance = app_data.get("compliance", [])

    # Compute
    if strategy == "refactor" and architecture == "microservices":
        compute = "ecs_fargate"
    elif strategy == "rehost":
        compute = "ec2"
    elif peak_tps > settings.bp_container_min_peak_tps:
        compute = "ecs_fargate"
    elif peak_tps < settings.bp_lambda_suggest_max_peak_tps and tier == "tier_3":
        compute = "lambda"
    else:
        compute = "ecs_fargate"

    # Database
    if db_engine in ("oracle", "postgresql", "postgres"):
        target_db = "aurora_postgresql"
    elif db_engine in ("mysql", "mariadb"):
        target_db = "aurora_mysql"
    elif db_engine in ("sql server", "sqlserver"):
        target_db = "rds_sqlserver"
    elif db_engine == "mongodb":
        target_db = "documentdb"
    elif db_engine == "dynamodb":
        target_db = "dynamodb"
    else:
        target_db = "aurora_postgresql"

    # Migration tool
    if db_engine in ("oracle", "sql server", "sqlserver"):
        migration_tool = "sct_dms"
    elif db_engine in ("postgresql", "postgres", "mysql", "mariadb"):
        migration_tool = "dms"
    elif db_engine == "mongodb":
        migration_tool = "native_dump"
    else:
        migration_tool = "dms"

    # Deployment
    if tier == "tier_1":
        deploy = "blue_green"
        multi_az = "yes"
        backup_days = settings.bp_backup_days_tier1
    elif tier == "tier_2":
        deploy = "blue_green"
        multi_az = "yes"
        backup_days = settings.bp_backup_days_tier2
    else:
        deploy = "rolling"
        multi_az = "no"
        backup_days = settings.bp_backup_days_tier3

    return {
        "account": "create_new",
        "account_id": "",
        "target_regions": [settings.aws_region],
        "vpc": "create_new",
        "vpc_id": "",
        "dns_route53": "yes_new",
        "compute_service": compute,
        "target_database": target_db,
        "migration_tool": migration_tool,
        "cicd_provider": "codepipeline",
        "deployment_strategy": deploy,
        "multi_az": multi_az,
        "backup_retention_days": backup_days,
        "encryption": "aws_managed" if not compliance else "cmk",
        "monitoring": ["cloudwatch"],
        "transformation_steps": _suggest_transformation_steps(strategy, language, app_data.get("runtime", {})),
        "testing_strategy": _suggest_testing(tier, peak_tps, compliance),
    }


def _suggest_transformation_steps(strategy: str, language: str, runtime: dict) -> list[str]:
    """Suggest ATX transformation definition names based on strategy and language."""
    steps = []
    lang = language.lower()
    jdk = runtime.get("jdk_version", "")
    dotnet = runtime.get("dotnet_version", "")

    if strategy in ("retire", "retain"):
        return []

    # Names are real ATX managed definitions (atx custom def list). This heuristic only
    # stands in until an assessment exists; the assessment's recommendation replaces it.
    # Java
    if lang == "java":
        if jdk in ("1.8", "8", "11"):
            steps.append("AWS/java-version-upgrade")
        framework = runtime.get("app_server", "").lower()
        if "spring" in framework:
            steps.append("AWS/spring-boot-version-upgrade")

    # .NET
    if lang in ("c#", "asp.net"):
        if dotnet and dotnet.startswith("4"):
            steps.append("AWS/dotnet-modernization")

    return steps


def _suggest_testing(tier: str, peak_tps: int, compliance: list) -> list[str]:
    """Suggest testing strategy based on app characteristics."""
    tests = ["unit", "integration", "smoke"]

    if peak_tps > settings.bp_load_test_min_peak_tps:
        tests.append("load")

    if tier in ("tier_1", "tier_2"):
        tests.append("e2e")

    if compliance:
        tests.append("security")

    return tests
