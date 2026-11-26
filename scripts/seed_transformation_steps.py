#!/usr/bin/env python3
"""Seed META.transformation_steps for an app from a checked-in units JSON.

transformation_steps is engineer/spec input derived from the app's units.md;
no automated workflow phase produces it. Without it, start_code_transformation
no-ops and LaunchTransformation hangs on its waitForTaskToken until the 4h
timeout. This script is the reproducible producer: it reads the committed
list[dict] of {td_name, additional_context, target_version} and writes it onto
the app's META item. Idempotent — safe to re-run.

Usage:
    python3 scripts/seed_transformation_steps.py \
        --app-id APP-EXAMPLE \
        --steps path/to/transformation_steps.json \
        [--table <prefix>_portfolio] [--region REGION] [--profile NAME]

--table defaults to $DYNAMODB_TABLE, else {PROJECT_PREFIX}_portfolio (hyphens ->
underscores, as Terraform names it); --region defaults to $AWS_REGION.

Pair with backend/src/api/v1/workflow.py (_reset_app_for_new_run keeps
transformation_steps) so the seed survives a fresh /workflow/start run.
"""
import argparse
import json
import os
import sys

import boto3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--steps", required=True, help="Path to the units JSON (list[dict])")
    prefix = os.environ.get("PROJECT_PREFIX", "")
    default_table = os.environ.get("DYNAMODB_TABLE") or (f"{prefix.replace('-', '_')}_portfolio" if prefix else "")
    ap.add_argument("--table", default=default_table,
                    help="DynamoDB table (default: $DYNAMODB_TABLE, else {PROJECT_PREFIX}_portfolio)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
                    help="AWS region (default: $AWS_REGION)")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()
    if not args.table or not args.region:
        ap.error("--table and --region are required (or set PROJECT_PREFIX / DYNAMODB_TABLE and AWS_REGION)")

    with open(args.steps) as fh:
        steps = json.load(fh)
    if not isinstance(steps, list) or not steps:
        print(f"ERROR: {args.steps} must be a non-empty JSON list", file=sys.stderr)
        return 2
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not s.get("td_name"):
            print(f"ERROR: step {i} missing td_name: {s!r}", file=sys.stderr)
            return 2

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    table = session.resource("dynamodb", region_name=args.region).Table(args.table)

    table.update_item(
        Key={"PK": f"APP#{args.app_id}", "SK": "META"},
        UpdateExpression="SET transformation_steps = :s",
        ExpressionAttributeValues={":s": steps},
    )
    print(f"Seeded {len(steps)} transformation_steps onto APP#{args.app_id}/META in {args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
