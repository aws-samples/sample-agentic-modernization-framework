"""
Discovery API endpoints.
Handles discovery export upload + ingest (POST /upload).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, UploadFile

from backend.src.auth.rbac import Role, require_role
from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, generic_error_message

logger = logging.getLogger(__name__)
router = APIRouter()


# ── POST /upload — accept ADS export CSVs and stage to S3 ─────────────

# CSVs the aws_ads adapter recognizes (full_exports/ subfolder)
_ALLOWED_FULL = {
    "server_inventory.csv",
    "server_performance_metrics.csv",
    "network_data_full.csv",
    "database_data_full.csv",
}
# MPA fallback CSVs (mpa_exports/ subfolder)
_ALLOWED_MPA = {"vmware_data_mpa.csv"}


@router.post("/upload")
async def upload_discovery_export(
    files: list[UploadFile] = File(...),
    user=require_role(Role.ENGINEER),
):
    """Upload and ingest discovery export files in one shot.

    Parses directly from memory (no S3 round-trip). Also stages to S3 for audit.
    Uses batch_writer for fast DynamoDB writes.
    """
    try:
        bucket = settings.s3_bucket
        run_id = uuid.uuid4().hex[:12]
        s3 = aws.client("s3") if bucket else None

        uploaded: list[dict] = []
        skipped: list[dict] = []

        # Write files to temp dir for adapter parsing
        tmp_dir = Path(tempfile.mkdtemp(prefix="discovery_"))
        (tmp_dir / "full_exports").mkdir(exist_ok=True)
        (tmp_dir / "mpa_exports").mkdir(exist_ok=True)

        source_type = "aws_ads"

        for f in files:
            name = os.path.basename(f.filename or "")
            body = await f.read()

            if name in _ALLOWED_FULL:
                local_path = tmp_dir / "full_exports" / name
                s3_key = f"discovery/l1/{run_id}/full_exports/{name}"
            elif name in _ALLOWED_MPA:
                local_path = tmp_dir / "mpa_exports" / name
                s3_key = f"discovery/l1/{run_id}/mpa_exports/{name}"
            else:
                skipped.append({"name": name, "reason": "filename not recognized"})
                continue

            # Write locally for parsing
            local_path.write_bytes(body)

            # Stage to S3 (non-blocking)
            if s3:
                try:
                    await asyncio.to_thread(
                        s3.put_object, Bucket=bucket, Key=s3_key, Body=body, ContentType="text/csv"
                    )
                except Exception as _err:
                    logger.debug("best-effort step ignored: %s", _err)

            uploaded.append({"name": name, "key": s3_key, "bytes": len(body)})

        if not uploaded:
            return ApiResponse(status="error", data={"message": "No recognized files uploaded"})

        # Parse directly from temp dir, then ingest into DynamoDB (batch_writer) —
        # CPU + boto3 work, so off the event loop.
        def _parse_and_ingest() -> dict:
            from backend.src.connectors.adapters.aws_ads import parse_discovery_export

            data = parse_discovery_export(str(tmp_dir))

            from backend.src.connectors.ingest import ingest_discovery_data

            return ingest_discovery_data(data, source=source_type)

        result = await asyncio.to_thread(_parse_and_ingest)

        # Cleanup temp
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)

        return ApiResponse(
            data={
                "run_id": run_id,
                "source": source_type,
                "uploaded": uploaded,
                "skipped": skipped,
                **result,
            }
        )
    except Exception as exc:
        logger.error("Discovery upload failed: %s", exc, exc_info=True)
        return ApiResponse(status="error", data={"message": generic_error_message(exc)})
