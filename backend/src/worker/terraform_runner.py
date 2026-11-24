"""
Terraform runner — worker MODE=infra (InfrastructureValidate state).

Runs AFTER provision_infrastructure has staged the generated Terraform to
`s3://{bucket}/applications/{app_id}/execution/infrastructure/`. This worker:

  1. Downloads the staged Terraform into a workdir (preserving modules/ layout)
  2. Drops a `_amf_local_backend_override.tf` (worker-only, never uploaded) that
     swaps the generated S3 backend for a local one — the target account's state
     bucket is neither reachable nor ours to touch, and `plan` refuses to run
     against a backend that was skipped with `init -backend=false`. Then runs
     `terraform init`, `terraform validate`, and `terraform plan -no-color -lock=false`
     — plan uses THIS task's role creds (the UAT account), never the app's
     target_account, against empty local state, and is best-effort: a plan that
     reports diffs or provider/auth notes is still a success.
  3. Uploads validate.txt / plan.txt / terraform-run.log back to the same
     execution/infrastructure/ S3 prefix, and writes an EXECUTION#infra-validate
     record so the review UI can surface the result.
  4. Resumes Step Functions via SendTaskSuccess (SendTaskFailure only on an
     internal error — e.g. nothing staged — never on a plan with findings).

Nothing is applied by this dry run. With INFRA_ACTION=apply|destroy (spec 17,
the human-approved Deploy / explicit Tear down) `run_terraform` hands over to
`deploy_runner.run_deploy` instead.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
import tempfile
import time
from pathlib import Path

from backend.src.config import settings
from backend.src.core.exposure_policy import evaluate as evaluate_exposure
from backend.src.worker.argsafe import confined
from backend.src.worker.config import (
    APP_ID,
    ATX_S3_OUTPUT_BUCKET,
    RUN_ID,
    ddb_table,
    logger,
    s3_client,
)
from backend.src.worker.sfn_callback import (
    finalize_job_once,
    resolve_token,
    send_failure,
    send_success,
)

_PREFIX = f"applications/{APP_ID}/execution/infrastructure/"
_TIMEOUT = settings.terraform_cmd_timeout_seconds  # per terraform command
_BACKEND_OVERRIDE = "_amf_local_backend_override.tf"
_RUN_OUTPUTS = ("validate.txt", "plan.txt", "terraform-run.log", "exposure.json")
# Override files merge over the generated backend.tf, so the committed IaC keeps
# its real S3 backend while this dry run plans against throwaway local state.
_BACKEND_OVERRIDE_TF = 'terraform {\n  backend "local" {}\n}\n'


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a terraform command, returning (exit_code, combined_output)."""
    logger.info("terraform: %s", " ".join(cmd))
    try:
        # Literal terraform argv from this module (init/validate/plan/show), cwd = the job's temp workdir.
        p = subprocess.run(  # nosec B603 — list argv, no shell, no external strings, config timeout
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=_TIMEOUT
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {_TIMEOUT}s: {' '.join(cmd)}"
    except Exception as exc:  # noqa: BLE001
        return 1, f"error running {' '.join(cmd)}: {exc}"


def _download_staged(workdir: Path) -> int:
    """Download the staged Terraform from S3 into workdir, preserving layout."""
    count = 0
    token = None
    while True:
        kw = {"Bucket": ATX_S3_OUTPUT_BUCKET, "Prefix": _PREFIX}
        if token:
            kw["ContinuationToken"] = token
        page = s3_client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(_PREFIX):]
            # Skip our own previously-uploaded run outputs.
            if not rel or rel in _RUN_OUTPUTS:
                continue
            try:  # an object key must not write outside the workdir (`../`, absolute)
                dest = confined(workdir, rel)
            except ValueError as exc:
                logger.warning("skipping staged object %s: %s", key, exc)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3_client.download_file(ATX_S3_OUTPUT_BUCKET, key, str(dest))
            count += 1
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return count


def _upload(name: str, body: str) -> None:
    s3_client.put_object(
        Bucket=ATX_S3_OUTPUT_BUCKET, Key=f"{_PREFIX}{name}",
        Body=body.encode("utf-8"), ContentType="text/plain",
    )


def _status(validate_ok: bool, plan_exit: int, exposure: list | None = None, waived: bool = False) -> str:
    if not validate_ok:
        return "invalid"
    if plan_exit != 0:
        return "plan_failed"
    return "exposed" if exposure and not waived else "validated"


def _exposure_waived() -> bool:
    """A public endpoint is an explicit, reviewed per-app opt-in (META expose_publicly)."""
    try:
        meta = ddb_table.get_item(Key={"PK": f"APP#{APP_ID}", "SK": "META"}).get("Item", {}) or {}
    except Exception as exc:  # noqa: BLE001 — unknown → not waived
        logger.warning("META read failed for exposure waiver: %s", exc)
        return False
    return str(meta.get("expose_publicly", "")).strip().lower() in ("true", "1", "yes")


def _record(validate_ok: bool, plan_exit: int, files: list[str], exposure: list | None = None,
            waived: bool = False) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    status = _status(validate_ok, plan_exit, exposure, waived)
    try:
        ddb_table.put_item(Item={
            "PK": f"APP#{APP_ID}", "SK": f"EXECUTION#infra-validate#{now}",
            "GSI1PK": "TYPE#EXECUTION", "GSI1SK": f"{APP_ID}#infra-validate",
            "app_id": APP_ID, "kind": "infra-validate",
            "status": status,
            "validate_ok": validate_ok, "plan_exit": plan_exit,
            "exposure_findings": exposure or [], "exposure_waived": waived,
            "s3_prefix": f"s3://{ATX_S3_OUTPUT_BUCKET}/{_PREFIX}",
            "files": files, "created_at": now,
        })
    except Exception as exc:  # noqa: BLE001 — never block the callback on the record
        logger.warning("infra-validate record write failed: %s", exc)
    # Mirror validate result onto META.exec_infra_status so the UI's status pill
    # reflects reality (previously stuck on "committed" even when validate failed).
    try:
        ddb_table.update_item(
            Key={"PK": f"APP#{APP_ID}", "SK": "META"},
            UpdateExpression="SET exec_infra_status = :s, exec_infra_validated_at = :n",
            ExpressionAttributeValues={":s": status, ":n": now},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("exec_infra_status mirror failed: %s", exc)


def run_terraform() -> None:
    from backend.src.worker.config import INFRA_ACTION

    if INFRA_ACTION in ("apply", "destroy"):
        from backend.src.worker.deploy_runner import run_deploy

        run_deploy(INFRA_ACTION)
        return
    token = resolve_token(RUN_ID)
    log: list[str] = []

    with tempfile.TemporaryDirectory(prefix="tf-") as tmp:
        workdir = Path(tmp)
        try:
            n = _download_staged(workdir)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to download staged Terraform: %s", exc)
            if finalize_job_once(RUN_ID, "FAILED"):
                send_failure(token, "TerraformStageMissing", f"Could not download staged IaC: {exc}")
            return
        if n == 0:
            msg = f"No staged Terraform found under {_PREFIX} — run provision_infrastructure first."
            logger.error(msg)
            _upload("terraform-run.log", msg)
            if finalize_job_once(RUN_ID, "FAILED"):
                send_failure(token, "TerraformStageMissing", msg)
            return
        logger.info("downloaded %d staged Terraform file(s) to %s", n, workdir)

        (workdir / _BACKEND_OVERRIDE).write_text(_BACKEND_OVERRIDE_TF)
        init_rc, init_out = _run(["terraform", "init", "-input=false", "-no-color"], workdir)
        log.append(f"$ terraform init  (S3 backend overridden → local state for this dry run)\n{init_out}")

        val_rc, val_out = _run(["terraform", "validate", "-no-color"], workdir)
        log.append(f"$ terraform validate\n{val_out}")
        validate_ok = val_rc == 0

        # Plan uses THIS task's role (UAT account). Best-effort: capture output
        # even when provider/data-source calls fail — a plan with findings or
        # auth notes is still a successful step, not a workflow failure.
        plan_rc, plan_out = _run(["terraform", "plan", "-no-color", "-input=false", "-lock=false", "-compact-warnings",
                                  "-out=amf.tfplan"], workdir)
        log.append(f"$ terraform plan\n{plan_out}")

        # Exposure policy on the PLANNED state (every app, same rule): no
        # internet-facing LB, no 0.0.0.0/0 ingress, no public IPs / databases /
        # buckets / unauthenticated endpoints unless META expose_publicly.
        exposure, waived = [], False
        if plan_rc == 0:
            show_rc, show_out = _run(["terraform", "show", "-json", "amf.tfplan"], workdir)
            try:
                exposure = evaluate_exposure(json.loads(show_out)) if show_rc == 0 else []
            except ValueError:
                log.append("exposure check: could not parse `terraform show -json` output")
            waived = bool(exposure) and _exposure_waived()
            log.append("$ exposure policy\n" + (json.dumps(exposure, indent=2) if exposure else "no exposure findings")
                       + ("\n(waived: META expose_publicly=true)" if waived else ""))

        _upload("validate.txt", val_out or "(no output)")
        _upload("plan.txt", plan_out or "(no output)")
        _upload("exposure.json", json.dumps({"findings": exposure, "waived": waived}, indent=2))
        _upload("terraform-run.log", "\n\n".join(log))
        _record(validate_ok, plan_rc, ["validate.txt", "plan.txt", "exposure.json", "terraform-run.log"], exposure, waived)
        logger.info("terraform run complete: init=%s validate=%s plan=%s exposure=%d waived=%s",
                    init_rc, val_rc, plan_rc, len(exposure), waived)

    # The worker ran to completion — resume the workflow. Validate/plan findings
    # are captured in S3, not treated as an internal failure.
    if finalize_job_once(RUN_ID, "COMPLETED"):
        send_success(token, {
            "app_id": APP_ID, "status": _status(validate_ok, plan_rc, exposure, waived),
            "validate_ok": validate_ok, "plan_exit": plan_rc,
            "exposure_findings": [f"{f['address']}: {f['detail']}" for f in exposure][:20], "exposure_waived": waived,
            "s3_prefix": f"s3://{ATX_S3_OUTPUT_BUCKET}/{_PREFIX}",
        })
