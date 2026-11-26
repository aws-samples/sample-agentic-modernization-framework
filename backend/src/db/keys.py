"""
Single-table key builders (Requirements 32.1, 32.2).

The one authoritative source for how every entity maps to (PK, SK) and to the
GSI1 (GSI1PK = TYPE#<entity>, GSI1SK = <app_id>#<discriminator>) index. All
accessors and callers should build keys through this module so the physical
layout lives in exactly one place.

Physical conventions (matching the deployed table):
  PK   = "APP#<app_id>"                     (application is the aggregate root)
  SK   = per-entity discriminator (see below)
  GSI1PK = "TYPE#<ENTITY>"                  (list-all-of-a-type)
  GSI1SK = "<app_id>#<discriminator>"       (stable ordering within a type)
"""

from __future__ import annotations

from dataclasses import dataclass

# ── GSI1 type partitions (list-all-of-a-kind) ─────────────────────────

TYPE_APP = "TYPE#APP"
TYPE_MACHINE = "TYPE#MACHINE"
TYPE_WORKFLOW = "TYPE#WORKFLOW"
TYPE_JOB = "TYPE#JOB"
TYPE_ANALYSIS = "TYPE#ANALYSIS"
TYPE_DESIGN = "TYPE#DESIGN"
TYPE_PLAN = "TYPE#PLAN"
TYPE_TASK_TOKEN = "TYPE#TASK_TOKEN"  # nosec B105 — DynamoDB GSI1 partition label, not a secret


def app_pk(app_id: str) -> str:
    """Partition key for everything belonging to an application."""
    return f"APP#{app_id}"


def app_id_from_pk(pk: str) -> str:
    """Inverse of app_pk."""
    return pk[len("APP#"):] if pk.startswith("APP#") else pk


@dataclass(frozen=True)
class Key:
    """A resolved (PK, SK) pair, optionally carrying its GSI1 attributes."""

    pk: str
    sk: str
    gsi1pk: str | None = None
    gsi1sk: str | None = None

    def as_key(self) -> dict:
        """Return the primary key dict for get_item/update_item/delete_item."""
        return {"PK": self.pk, "SK": self.sk}

    def gsi1_attrs(self) -> dict:
        """Return the GSI1 attributes to merge into an item on write (if any)."""
        out: dict = {}
        if self.gsi1pk is not None:
            out["GSI1PK"] = self.gsi1pk
        if self.gsi1sk is not None:
            out["GSI1SK"] = self.gsi1sk
        return out


# ── Entity key builders ───────────────────────────────────────────────


def application(app_id: str) -> Key:
    """Application aggregate-root META record."""
    return Key(app_pk(app_id), "META", TYPE_APP, app_id)


def machine(app_id: str, machine_id: str) -> Key:
    return Key(app_pk(app_id), f"MACHINE#{machine_id}", TYPE_MACHINE, f"{app_id}#{machine_id}")


def repository(app_id: str, repo_key: str) -> Key:
    return Key(app_pk(app_id), f"REPO#{repo_key}")


def owner(app_id: str, owner_key: str) -> Key:
    return Key(app_pk(app_id), f"OWNER#{owner_key}")


def software(app_id: str, sw_key: str) -> Key:
    return Key(app_pk(app_id), f"SW#{sw_key}")


def workflow_active(app_id: str) -> Key:
    """The single active workflow-state record for an application."""
    return Key(app_pk(app_id), "WORKFLOW#ACTIVE", TYPE_WORKFLOW, app_id)


def task_token(app_id: str, step: str) -> Key:
    """A pending approval / callback token for a workflow step."""
    return Key(app_pk(app_id), f"TASK_TOKEN#{step}", TYPE_TASK_TOKEN, f"{app_id}#{step}")


def analysis(app_id: str, analysis_type: str, ts: str) -> Key:
    """A single analysis/report metadata record (newest sorts last by ts)."""
    return Key(app_pk(app_id), f"ANALYSIS#{analysis_type}#{ts}", TYPE_ANALYSIS, f"{app_id}#{analysis_type}")


def analysis_prefix(analysis_type: str) -> str:
    """SK begins_with prefix to fetch all runs of one analysis type."""
    return f"ANALYSIS#{analysis_type}#"


def design(app_id: str, version: str) -> Key:
    """A versioned design document record."""
    return Key(app_pk(app_id), f"DESIGN#{version}", TYPE_DESIGN, f"{app_id}#{version}")


def job(app_id: str, job_id: str) -> Key:
    """An ATX/ECS job record."""
    return Key(app_pk(app_id), f"JOB#{job_id}", TYPE_JOB, f"{app_id}#{job_id}")


def plan(app_id: str, plan_id: str) -> Key:
    """A migration/wave plan record."""
    return Key(app_pk(app_id), f"PLAN#{plan_id}", TYPE_PLAN, f"{app_id}#{plan_id}")
