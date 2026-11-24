"""
Application completeness scoring and phase gate readiness.
Calculates how much discovery/requirements data has been captured
and determines if an app is ready for the next phase.
"""

from __future__ import annotations

# Fields grouped by category with weights (duplicated in tool_strategy.py — keep in sync)
COMPLETENESS_FIELDS = {
    "basic": {
        "weight": 15,
        "fields": [
            ("app_name", "Application name"),
            ("description", "Description"),
            ("business_unit", "Business unit"),
            ("tier", "Application tier"),
        ],
    },
    "infrastructure": {
        "weight": 15,
        "fields": [
            ("machine_ids", "Machines assigned (at least 1)"),
            ("machine_count", "Machine count > 0"),
        ],
    },
    "technical": {
        "weight": 15,
        "fields": [
            ("language", "Programming language"),
            ("framework", "Framework"),
            ("architecture", "Architecture type"),
        ],
    },
    "requirements": {
        "weight": 20,
        "fields": [
            ("compliance", "Compliance requirements"),
            ("rpo", "RPO (Recovery Point Objective)"),
            ("rto", "RTO (Recovery Time Objective)"),
            ("target_region", "Target AWS region"),
            ("data_classification", "Data classification"),
        ],
    },
    "ownership": {
        "weight": 5,
        "fields": [
            ("owners", "At least one owner assigned"),
        ],
    },
    "code": {
        "weight": 10,
        "fields": [
            ("repos", "At least one repository linked"),
        ],
    },
    "operational": {
        "weight": 10,
        "fields": [
            ("change_freeze_windows", "Change freeze windows"),
            ("dependencies_upstream", "Upstream dependencies"),
            ("dependencies_downstream", "Downstream dependencies"),
        ],
    },
    "migration_readiness": {
        "weight": 10,
        "fields": [
            ("licensing", "Licensing details (BYOL, open-source, enterprise)"),
            ("db_migration_method", "Database migration method"),
            ("startup_shutdown_procedures", "Startup/shutdown procedures"),
        ],
    },
}

# Phase gates — mandatory fields for each phase. The single source of truth;
# infra/environments/agentcore/lambda-src/tool_strategy.py duplicates it (the
# Lambda cannot import backend/) — keep in sync (enforced by a unit test).
PHASE_GATES = {
    "assessment": {
        "label": "Assessment",
        "mandatory": ["app_name", "tier", "machine_ids", "compliance", "rpo", "rto", "target_region"],
        "description": "Run AI-powered migration assessment",
    },
    "wave_planning": {
        "label": "Wave Planning",
        "mandatory": [
            "app_name",
            "tier",
            "machine_ids",
            "compliance",
            "rpo",
            "rto",
            "target_region",
            "migration_strategy",
            "owners",
        ],
        "description": "Assign to migration wave",
    },
    "design": {
        "label": "Design Document",
        "mandatory": [
            "app_name",
            "tier",
            "machine_ids",
            "compliance",
            "rpo",
            "rto",
            "target_region",
            "migration_strategy",
            "owners",
            "data_classification",
            "uptime_sla",
        ],
        "description": "Generate migration design document",
    },
    "transformation": {
        "label": "Code Transformation",
        "mandatory": [
            "app_name",
            "tier",
            "machine_ids",
            "compliance",
            "rpo",
            "rto",
            "target_region",
            "migration_strategy",
            "owners",
            "data_classification",
            "repos",
        ],
        "description": "Run ATX code transformation",
    },
}


def calculate_completeness(app_data: dict) -> dict:
    """Calculate completeness percentage for an application.

    Returns:
        Dict with overall percentage, category scores, and missing fields.
    """
    total_weight = 0
    earned_weight = 0
    missing = []
    categories = {}

    for category, config in COMPLETENESS_FIELDS.items():
        weight = config["weight"]
        fields = config["fields"]
        total_weight += weight

        filled = 0
        category_missing = []

        for field_name, description in fields:
            is_filled = _is_filled(field_name, _field_value(app_data, field_name))
            if is_filled:
                filled += 1
            else:
                category_missing.append({"field": field_name, "description": description})
                missing.append({"field": field_name, "description": description, "category": category})

        category_pct = round((filled / len(fields)) * 100) if fields else 100
        category_earned = round(weight * (filled / len(fields))) if fields else weight
        earned_weight += category_earned

        categories[category] = {
            "percentage": category_pct,
            "filled": filled,
            "total": len(fields),
            "missing": category_missing,
        }

    overall_pct = round((earned_weight / total_weight) * 100) if total_weight else 0

    return {
        "percentage": overall_pct,
        "categories": categories,
        "missing_fields": missing,
        "missing_count": len(missing),
        "total_fields": sum(len(c["fields"]) for c in COMPLETENESS_FIELDS.values()),
    }


# owners / repos are stored as OWNER# / REPO# sibling items, not on META.
# read_full_app attaches them as lists; a caller holding only META (or the
# tool_strategy Lambda, which counts the siblings) supplies owner_count /
# repo_count instead. Either form counts — reading only META.owners reported
# "no owner" for apps that had OWNER# items.
_COUNT_FALLBACK = {"owners": "owner_count", "repos": "repo_count"}


def _field_value(app_data: dict, field_name: str):
    value = app_data.get(field_name)
    if field_name in _COUNT_FALLBACK and not value:
        value = app_data.get(_COUNT_FALLBACK[field_name])
    return value


def _is_filled(field_name: str, value) -> bool:
    """Check if a field has a meaningful value."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value > 0
    return bool(value)
