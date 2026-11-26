"""
Skills API — View and upload agent skill files.

Skills are the per-phase playbooks (backend/resources/skills/*.md) that
harness.tf composes into the single AgentCore Harness system prompt.

Workflow:
  1. Upload skill .md files via this API → written locally + staged on S3
  2. Commit them and run `terraform apply` (or scripts/setup.sh) → the Harness
     prompt is rebuilt; nothing reads skills at runtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, File, UploadFile

from backend.src.auth.rbac import Role, require_role
from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, error_response, internal_error

logger = logging.getLogger(__name__)
router = APIRouter()

SKILLS_DIR = Path(__file__).resolve().parents[3] / "resources" / "skills"


# ── List skills ────────────────────────────────────────────────────────


@router.get("/")
def list_skills():
    """List all available skills (loaded from local filesystem)."""
    try:
        from backend.resources.skills import list_skills as _list_skills

        skills = _list_skills()
        return ApiResponse(data=skills)
    except Exception as exc:
        logger.error("List skills failed: %s", exc)
        return internal_error(exc, code="LIST_FAILED")


# ── Get skill content ──────────────────────────────────────────────────


@router.get("/{name}")
def get_skill(name: str):
    """Get the content of a specific skill file."""
    if not _SKILL_NAME_RE.fullmatch(name):
        return error_response(f"Skill '{name}' not found", code="NOT_FOUND")
    try:
        from backend.resources.skills import load_skill

        content = load_skill(name)
        return ApiResponse(data={"name": name, "content": content})
    except FileNotFoundError:
        return error_response(f"Skill '{name}' not found", code="NOT_FOUND")
    except Exception as exc:
        logger.error("Get skill failed: %s", exc)
        return internal_error(exc, code="GET_FAILED")


# ── Upload/update skill ───────────────────────────────────────────────


_SKILL_FILE_RE = re.compile(r"^[a-z0-9_-]+\.md$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# The phase playbooks harness.tf composes into the Harness system prompt
# (`harness_skill_files`). Domain contract, not a tunable: an upload must never
# replace one — they change only through a reviewed commit.
# tests/unit/test_skills_upload.py keeps this in sync with harness.tf.
CORE_SKILLS = frozenset({
    "discovery", "risk_scoring", "grouping", "assessment", "planning",
    "design", "execution", "validation", "handover",
})


def _check_upload_name(filename: str) -> tuple[str, str]:
    """(safe file name, "") for an acceptable upload, else ("", why it was refused)."""
    name = os.path.basename(filename.replace("\\", "/"))
    if name != filename or not _SKILL_FILE_RE.fullmatch(name):
        return "", "File name must match [a-z0-9_-]+.md (no directories)"
    if name.removesuffix(".md") in CORE_SKILLS:
        return "", "Core Harness playbooks can't be replaced by upload (change them in a reviewed commit)"
    return name, ""


@router.post("/upload", dependencies=[require_role(Role.ADMIN)])
async def upload_skill(files: list[UploadFile] = File(...)):
    """Upload skill markdown files.

    Files are written directly to the local skills directory AND staged to S3.
    The Harness picks them up only after `terraform apply` (see module docstring).
    """
    try:
        uploaded = []
        skipped = []

        for f in files:
            raw_name = f.filename or ""
            name, reason = _check_upload_name(raw_name)
            if reason:
                skipped.append({"name": raw_name, "reason": reason})
                continue

            body = await f.read(settings.skill_upload_max_bytes + 1)
            if len(body) > settings.skill_upload_max_bytes:
                skipped.append({"name": raw_name, "reason": f"Larger than {settings.skill_upload_max_bytes} bytes"})
                continue
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"name": raw_name, "reason": "Must be UTF-8 text"})
                continue
            skill_name = name.removesuffix(".md")

            # Write to local skills directory (immediate effect for local dev).
            # Belt and braces after _check_upload_name: the target must be a direct
            # child of SKILLS_DIR.
            local_path = (SKILLS_DIR / name).resolve()
            if local_path.parent != SKILLS_DIR.resolve():
                skipped.append({"name": raw_name, "reason": "Invalid file name"})
                continue
            local_path.write_bytes(body)

            # Also stage to S3 for Docker build reference
            bucket = settings.s3_bucket
            if bucket:
                try:
                    s3 = aws.client("s3")
                    await asyncio.to_thread(
                        s3.put_object,
                        Bucket=bucket,
                        Key=f"config/skills/{name}",
                        Body=body,
                        ContentType="text/markdown",
                        Metadata={"uploaded-at": datetime.now(UTC).isoformat()},
                    )
                except Exception as exc:
                    logger.warning("S3 staging failed (non-blocking): %s", exc)

            uploaded.append(
                {
                    "name": skill_name,
                    "file": name,
                    "bytes": len(body),
                    "lines": content.count("\n") + 1,
                }
            )

        return ApiResponse(
            data={
                "uploaded": uploaded,
                "skipped": skipped,
                "message": (
                    f"Uploaded {len(uploaded)} skill(s). Skills reach the Harness only through harness.tf: "
                    "commit them and run `terraform apply` (or scripts/setup.sh) to rebuild its prompt."
                ),
            }
        )

    except Exception as exc:
        logger.error("Upload skill failed: %s", exc)
        return internal_error(exc, code="UPLOAD_FAILED")
