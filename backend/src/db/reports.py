"""
Report_Store helper (Requirements 9.3, 33.1, 33.3, 33.5, 43).

Single source for writing phase reports/artifacts to S3 under the canonical
layout and for size-offloading large values out of DynamoDB.

Layout:
    applications/<app_id>/<phase>/<run_id>/<name>

DynamoDB stores only bounded metadata + an S3 reference. Any value that would
push an item near the 400 KB item-size limit is written to S3 and replaced with
a reference marker so the metadata record stays small.
"""

from __future__ import annotations

import logging

from backend.src.config import settings
from backend.src.core import aws

logger = logging.getLogger(__name__)

def _s3():
    return aws.client("s3")


def report_prefix(app_id: str, phase: str, run_id: str | None = None) -> str:
    base = f"applications/{app_id}/{phase}/"
    return f"{base}{run_id}/" if run_id else base


def _ttl(expires: int | None) -> int:
    """Clamp a requested presign lifetime to [presign_min_seconds, presign_max_seconds]."""
    value = settings.presign_ttl_seconds if expires is None else expires
    return max(settings.presign_min_seconds, min(value, settings.presign_max_seconds))


def list_reports(app_id: str, phase: str, expires: int | None = None, run_id: str | None = None) -> list[dict]:
    """List artifacts under a phase prefix with time-limited presigned URLs."""
    s3 = _s3()
    prefix = report_prefix(app_id, phase, run_id)
    listing = s3.list_objects_v2(Bucket=settings.s3_bucket, Prefix=prefix, MaxKeys=settings.report_list_max_keys)
    out = []
    for obj in listing.get("Contents", []):
        k = obj["Key"]
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": k},
            ExpiresIn=_ttl(expires),
        )
        out.append({"key": k, "name": k[len(prefix):], "size": obj.get("Size", 0), "url": url})
    return out


def presign(key: str, expires: int | None = None) -> str:
    """Presigned GET URL for a single stored artifact."""
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=_ttl(expires),
    )


def read(key: str) -> str:
    """Read an artifact body back as text."""
    return _s3().get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read().decode("utf-8")
