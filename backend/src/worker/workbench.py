"""
Workbench pipeline — runs ATX transformation steps with git push.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from backend.src.worker.argsafe import confined, safe_dir_name, safe_ref, safe_td_name
from backend.src.worker.atx import clone_repo_full, configure_git
from backend.src.worker.branches import resolve_push_branch
from backend.src.worker.config import (
    APP_ID,
    ATX_S3_OUTPUT_BUCKET,
    ATX_SHELL_TIMEOUT,
    PLAN_ID,
    START_FROM_STEP,
    STEPS_JSON,
    logger,
    read_full_app,
    s3_client,
)
from backend.src.worker.dockerfile_gen import ensure_dockerfile
from backend.src.worker.dynamo import update_plan_status, update_plan_step_status
from backend.src.worker.gitcmd import run_git
from backend.src.worker.scope import app_build_dir, atx_path, changed_paths, primary_repo_name, scope_outcome
from backend.src.worker.synthesis import call_workbench_synthesis

SECURITY_FIXER_TD = "AWS/security-issue-fixer"  # mirror: core/strategy_options.SECURITY_FIXER_TD
_ATX_CUSTOM_DIR = Path.home() / ".aws" / "atx" / "custom"
_STEP_ARTIFACTS = ("worklog.log", "git_instructions.md", "validation_summary.md")
_OVERALL_STATUS_RE = re.compile(r"^#*\s*OVERALL STATUS:\s*\**\s*([A-Za-z_ -]+?)\s*\**\s*$", re.M | re.I)


def parse_validation_status(text: str) -> str:
    """ATX validation_summary.md "## OVERALL STATUS: X" -> "X" (upper-cased), "" if absent."""
    m = _OVERALL_STATUS_RE.search(text or "")
    return m.group(1).strip().upper() if m else ""


def step_outcome(changed: bool, validation: str, build_command: str, branch: str) -> tuple[str, str]:
    """Honest per-step status + message.

    - ``no_change``: ATX produced no diff (e.g. "already Java 21", no SDK v1 found)
    - ``completed``: changes pushed AND verified (validation COMPLETE with a build command)
    - ``partial``:   changes pushed but unverified (validation PARTIAL/missing, or
                     no build command so nothing was compiled)
    """
    if not changed:
        return "no_change", (
            f"No change: ATX produced no diff (already compliant or nothing to transform); "
            f"validation {validation or 'n/a'}"
        )
    if validation == "COMPLETE" and build_command:
        return "completed", f"Pushed to {branch} — build verified (validation COMPLETE)"
    why = []
    if validation != "COMPLETE":
        why.append(f"validation {validation or 'missing'}")
    if not build_command:
        why.append("no build command, build not verified")
    return "partial", f"Pushed to {branch} — unverified: {', '.join(why)}"


def plan_final_status(failed_step: int, synthesis: dict) -> str:
    """Plan status: failed > completed_with_errors (synthesis failed) > completed."""
    if failed_step:
        return "failed"
    if (synthesis or {}).get("error"):
        return "completed_with_errors"
    return "completed"


def _git_head(repo_dir: Path) -> str:
    r = run_git(["rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _step_validation_status(pre_runs: set[str]) -> str:
    """OVERALL STATUS of the validation_summary.md written by the ATX run(s) of this step."""
    if not _ATX_CUSTOM_DIR.exists():
        return ""
    for run_dir in sorted(p for p in _ATX_CUSTOM_DIR.iterdir() if p.is_dir() and p.name not in pre_runs):
        summary = run_dir / "artifacts" / "validation_summary.md"
        if summary.exists():
            return parse_validation_status(summary.read_text(errors="replace"))
    return ""


def _detect_build_command(repo_dir: Path) -> str:
    """Pick a build command ATX can use to verify its own changes.

    ATX accepts a buildCommand and runs it to check that a transformation still
    compiles. When none is supplied it says so -- "The build command for this
    project is empty (not configured)" -- and completes the step regardless, so
    the pipeline reports success over code that has never been built.

    Deliberately compile-only rather than a full package or test run: ATX
    re-runs this on each repair iteration, and the projects seen so far have no
    tests at all (some apps have no src/test directory), so `verify`
    would spend agent minutes on packaging without checking anything more.
    Compilation is what catches the failures these transformations actually
    cause -- a javax import that should be jakarta, a removed API, a dependency
    whose major version moved.

    Returns "" when the build tool is unrecognised, which the caller logs as a
    warning rather than passing an empty value ATX would reject.
    """
    root = _build_command_in(repo_dir)
    if root:
        return root
    # Monorepos keep their builds in subprojects (unishop: MonoToMicroLegacy/
    # build.gradle, MonoToMicroLambda/pom.xml) — compile each one in its own dir.
    subs = [(d.name, cmd) for d in sorted(repo_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".") and (cmd := _build_command_in(d))]
    return " && ".join(f"(cd {name} && {cmd})" for name, cmd in subs[:4])


def _build_command_in(d: Path) -> str:
    if (d / "pom.xml").is_file():
        # -B for non-interactive output, -q so the diagnostics ATX reads are
        # errors rather than several hundred download lines.
        return "mvn -B -q compile"
    if (d / "gradlew").is_file():
        return "./gradlew --console=plain compileJava"
    if (d / "build.gradle").is_file() or (d / "build.gradle.kts").is_file():
        return "gradle --console=plain compileJava"
    return ""


def _snapshot_atx_runs() -> set[str]:
    """Names of ATX custom run dirs that already exist (to detect new ones)."""
    if not _ATX_CUSTOM_DIR.exists():
        return set()
    return {p.name for p in _ATX_CUSTOM_DIR.iterdir() if p.is_dir()}


def _upload_step_artifacts(step_num: int, pre_runs: set[str]) -> str:
    """Upload the ATX artifacts a step just produced to its transform prefix and
    return the S3 key of that step's validation_summary.md (or "").
    """
    if not _ATX_CUSTOM_DIR.exists():
        return ""
    new_runs = sorted(p for p in _ATX_CUSTOM_DIR.iterdir() if p.is_dir() and p.name not in pre_runs)
    s3_prefix = f"applications/{APP_ID}/execution/transformation/{PLAN_ID}"
    report_key = ""
    for run_dir in new_runs:
        for name in _STEP_ARTIFACTS:
            src = run_dir / "artifacts" / name
            if not src.exists():
                continue
            key = f"{s3_prefix}/artifacts/{run_dir.name}/{name}"
            try:
                s3_client.upload_file(str(src), ATX_S3_OUTPUT_BUCKET, key)
                if name == "validation_summary.md":
                    report_key = key
            except Exception as exc:
                logger.warning("step %d artifact upload failed (%s): %s", step_num, key, exc)
    return report_key


def run_workbench():
    """Run the workbench transformation pipeline."""
    logger.info("Workbench starting for %s (plan: %s, start_step: %d)", APP_ID, PLAN_ID, START_FROM_STEP)
    start_time = time.time()

    steps = json.loads(STEPS_JSON)
    steps.sort(key=lambda s: s.get("step_number", 0))
    steps_results = []

    workspace = Path("/atx-workspace")
    workspace.mkdir(exist_ok=True)
    configure_git()
    update_plan_status("running")

    failed_step = 0

    # The app's build_dir scopes ATX for its primary repo (spec 12).
    try:
        app = read_full_app(APP_ID) or {}
    except Exception as exc:  # noqa: BLE001 — unscoped (repo root) is today's behaviour
        logger.warning("app read failed (%s) — ATX runs on the whole repo", exc)
        app = {}
    build_dir, primary_repo = app_build_dir(app), primary_repo_name(app)

    try:
        for step in steps:
            step_num = step.get("step_number", 0)
            td_name = step.get("td_name", "")
            repo_name = step.get("repo_name", "")
            repo_url = step.get("repo_url", "")
            # Never push to the default branch: work on the app's migration branch
            # (same convention as the Lambda's IaC/CI-CD commits — worker/branches.py).
            repo_branch, repo_source_ref = resolve_push_branch(
                APP_ID, step.get("repo_branch", "main"), step.get("repo_source_ref", ""),
            )
            repo_review_base_branch = step.get("repo_review_base_branch", "")
            context = step.get("additional_context", "")

            if step_num < START_FROM_STEP:
                logger.info("Skipping step %d (already completed)", step_num)
                steps_results.append(
                    {"step_number": step_num, "status": "skipped", "td_name": td_name, "repo_name": repo_name}
                )
                continue

            logger.info("═══ Step %d: %s on %s ═══", step_num, td_name, repo_name)
            update_plan_step_status(step_num, "running")

            # Plan-step values reach git/ATX argv and the cwd: plain names only, repo dir inside the workspace.
            try:
                td_name = safe_td_name(td_name)
                repo_branch = safe_ref(repo_branch, "branch")
                repo_dir = confined(workspace, safe_dir_name(repo_name))
            except ValueError as exc:
                error_msg = f"Step inputs refused: {exc}"
                logger.error("Step %d: %s", step_num, error_msg)
                update_plan_step_status(step_num, "failed", error_msg)
                steps_results.append({"step_number": step_num, "status": "failed", "td_name": td_name,
                                      "repo_name": repo_name, "message": error_msg})
                failed_step = step_num
                break

            # Clone repo (full history for push)
            if not repo_dir.exists():
                if not clone_repo_full(
                    repo_url, repo_branch, repo_dir,
                    repo_source_ref, repo_review_base_branch,
                ):
                    error_msg = f"Clone failed for {repo_name}"
                    update_plan_step_status(step_num, "failed", error_msg)
                    steps_results.append(
                        {
                            "step_number": step_num,
                            "status": "failed",
                            "td_name": td_name,
                            "repo_name": repo_name,
                            "message": error_msg,
                        }
                    )
                    failed_step = step_num
                    break

            # Snapshot existing ATX run dirs so we can attribute the artifacts
            # this step produces (a fresh run dir) back to this step.
            pre_runs = _snapshot_atx_runs()
            pre_head = _git_head(repo_dir)

            # Run ATX transformation, scoped to build_dir for the app's primary repo.
            step_scope = build_dir if (not primary_repo or repo_name == primary_repo) else ""

            # ATX's security fixer only remediates findings it is handed, and AMF ships no
            # scanner (README "Security scanning": use Amazon Inspector), so it is skipped.
            if td_name == SECURITY_FIXER_TD:
                msg = ("Skipped: AWS/security-issue-fixer needs scanner findings and AMF runs no scanner "
                       "(see README, Security scanning).")
                logger.info("Step %d: %s", step_num, msg)
                update_plan_step_status(step_num, "skipped", msg)
                steps_results.append({"step_number": step_num, "status": "skipped", "td_name": td_name,
                                      "repo_name": repo_name, "message": msg})
                continue

            # List argv (no shell, no re-splitting): validated TD name, -p confined to repo_dir by atx_path.
            cmd = ["atx", "custom", "def", "exec", "-n", td_name, "-p", atx_path(repo_dir, step_scope),
                   "-x", "-t", "--do-not-learn"]
            cfg_path = None
            cfg: dict[str, str] = {}
            if context:
                cfg["additionalPlanContext"] = context

            # Give ATX a build command so it can VERIFY its own changes.
            # Without one it reports "The build command for this project is empty
            # (not configured)" and completes the step anyway, producing code that
            # has never been compiled. The step is allowed to say it succeeded,
            # which makes an unverified transform indistinguishable from a
            # verified one.
            #
            # A unit may name its own command; otherwise detect the build tool
            # from the repository so this works for the next workload too.
            build_command = step.get("build_command") or _detect_build_command(repo_dir)
            if build_command:
                cfg["buildCommand"] = build_command

            if cfg:
                # ATX accepts a config FILE (JSON or YAML) or key=value pairs. The
                # pair form splits on commas -- its own help shows
                #   "additionalPlanContext=...,buildCommand=mvn clean test"
                # -- so a unit instruction containing a comma or an '=' silently
                # corrupts the parse. Both of ours routinely do. Write JSON and
                # point at the file instead; that also avoids interpolating
                # hundreds of characters of prose into a shell argument.
                # mkstemp, not a predictable /tmp name: the sandboxed build user shares /tmp and could
                # pre-plant a symlink there.
                fd, tmp_name = tempfile.mkstemp(prefix=f"atx-config-step{step_num}-", suffix=".json")
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps(cfg))
                cfg_path = Path(tmp_name)
                cmd += ["--configuration", f"file://{cfg_path}"]
                logger.info(
                    "[ATX/step%d/%s] config via %s (context %d chars, build %r)",
                    step_num, repo_name, cfg_path, len(context or ""),
                    build_command or "none",
                )
                if not build_command:
                    logger.warning(
                        "[ATX/step%d/%s] no build command detected — ATX cannot "
                        "verify this transformation and the result will be "
                        "uncompiled", step_num, repo_name,
                    )

            logger.info("[DEBUG] ATX command: %s (cwd: %s)", cmd, repo_dir)
            result = subprocess.run(  # nosec B603 B607 — list argv; validated TD name; confined cwd and -p; timeout
                cmd,
                shell=False,
                cwd=str(repo_dir),
                timeout=ATX_SHELL_TIMEOUT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if result.stdout:
                lines = result.stdout.splitlines()
                # ATX prints a banner block (region, trust notice, learning
                # notice, managed-transformation notice) AFTER its real output,
                # so a plain tail slice captures only decoration and discards the
                # error. A 15-second exit-1 was undiagnosable for exactly this
                # reason. Log the head as well, and surface anything that looks
                # like a diagnosis under its own prefix so it is greppable.
                head, tail = lines[:60], lines[-40:]
                if len(lines) <= 100:
                    head, tail = lines, []
                for line in head:
                    logger.info("[ATX/step%d/%s] %s", step_num, repo_name, line)
                if tail:
                    logger.info(
                        "[ATX/step%d/%s] ... %d line(s) omitted ...",
                        step_num, repo_name, len(lines) - len(head) - len(tail),
                    )
                    for line in tail:
                        logger.info("[ATX/step%d/%s] %s", step_num, repo_name, line)

                # Pull the lines that actually explain a failure. Matched against
                # the whole output, not just the slice that got logged.
                diag = [
                    ln.strip()
                    for ln in lines
                    if re.search(
                        r"(?i)\b(error|failed|failure|denied|unauthorized|forbidden|"
                        r"quota|throttl|invalid|not found|unsupported|exception|"
                        r"timed? out|cannot|unable)\b",
                        ln,
                    )
                    and ln.strip()
                ]
                for ln in diag[-12:]:
                    logger.error("[ATX/step%d/%s/DIAG] %s", step_num, repo_name, ln)

            if result.returncode != 0:
                # Put the first diagnostic line in the message itself so the
                # reason reaches the plan step record and the UI, not just the
                # log group — a step that only says "exit 1" forces whoever is
                # debugging to go find the logs, if they were even captured.
                detail = ""
                if result.stdout:
                    picked = [d for d in diag if "trusted" not in d.lower()]
                    if picked:
                        detail = f": {picked[0][:300]}"
                error_msg = f"ATX failed (exit {result.returncode}){detail}"
                logger.error("Step %d: %s", step_num, error_msg)
                update_plan_step_status(step_num, "failed", error_msg)
                steps_results.append(
                    {
                        "step_number": step_num,
                        "status": "failed",
                        "td_name": td_name,
                        "repo_name": repo_name,
                        "message": error_msg,
                    }
                )
                failed_step = step_num
                break

            # Git: handle ATX's staging branch pattern
            # ATX may: (a) create a staging branch with changes, or (b) commit directly on current branch
            commit_msg = f"PlanId: {PLAN_ID} Step: {step_num} - {step.get('td_display_name', td_name)}"

            current_branch_result = run_git(
                ["branch", "--show-current"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            current_branch = current_branch_result.stdout.strip()
            logger.info("Step %d: current branch after ATX: %s (target: %s)", step_num, current_branch, repo_branch)

            if current_branch and current_branch != repo_branch:
                # ATX switched to a staging branch — merge changes back to original
                logger.info("Step %d: ATX created branch %s, merging to %s", step_num, current_branch, repo_branch)
                # Remove build artifacts from ATX's branch before merging
                run_git(
                    ["rm", "-f", "--cached", "atx-run.log", "build.log"], cwd=str(repo_dir), capture_output=True
                )
                run_git(
                    ["rm", "-rf", "--cached", "ATXDocumentation/", "__pycache__/"],
                    cwd=str(repo_dir),
                    capture_output=True,
                )
                run_git(["clean", "-fd", "__pycache__"], cwd=str(repo_dir), capture_output=True)
                # Switch back to target branch and merge
                run_git(["checkout", "-f", repo_branch, "--"], cwd=str(repo_dir), capture_output=True)
                merge_result = run_git(
                    ["merge", current_branch, "--no-edit", "-m", commit_msg],
                    cwd=str(repo_dir),
                    capture_output=True,
                    text=True,
                )
                if merge_result.returncode != 0:
                    logger.warning("Step %d: merge conflict, attempting auto-resolve", step_num)
                    run_git(["checkout", "--theirs", "."], cwd=str(repo_dir), capture_output=True)
                    run_git(["add", "-A"], cwd=str(repo_dir), capture_output=True)
                    run_git(
                        ["commit", "--no-edit", "-m", commit_msg], cwd=str(repo_dir), capture_output=True
                    )
            else:
                # ATX committed directly or left uncommitted changes
                # Exclude build artifacts
                run_git(["add", "-A"], cwd=str(repo_dir), capture_output=True)
                run_git(
                    ["reset", "--", "ATXDocumentation/", "atx-run.log", "build.log"],
                    cwd=str(repo_dir),
                    capture_output=True,
                )
                diff_result = run_git(
                    ["diff", "--cached", "--quiet"], cwd=str(repo_dir), capture_output=True
                )
                if diff_result.returncode != 0:
                    run_git(["commit", "-m", commit_msg], cwd=str(repo_dir), capture_output=True)
                else:
                    logger.info("Step %d: no changes to commit (ATX may have failed internally)", step_num)

            # Did ATX actually change anything? Measured before our own Dockerfile
            # commit below so a generated Dockerfile never masks a no-op transform.
            changed = _git_head(repo_dir) != pre_head

            # Ensure a buildable container image exists for downstream CI/CD.
            # ATX may not emit a root Dockerfile; generate a standard one if absent
            # (idempotent — only the first step that needs it writes the file).
            df = ensure_dockerfile(repo_dir)
            if df.get("status") == "generated":
                run_git(["add", "Dockerfile"], cwd=str(repo_dir), capture_output=True)
                run_git(
                    ["commit", "-m", f"chore: add {df['stack']} Dockerfile for CI/CD build"],
                    cwd=str(repo_dir),
                    capture_output=True,
                )

            # Push
            push_result = run_git(
                ["push", "origin", repo_branch],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if push_result.returncode != 0:
                push_result = run_git(
                    ["push", "origin", f"HEAD:{repo_branch}"],
                    cwd=str(repo_dir),
                    capture_output=True,
                    text=True,
                )
                if push_result.returncode != 0:
                    error_msg = f"Git push failed: {push_result.stderr.strip()}"
                    logger.error("Step %d: %s", step_num, error_msg)
                    update_plan_step_status(step_num, "failed", error_msg)
                    steps_results.append(
                        {
                            "step_number": step_num,
                            "status": "failed",
                            "td_name": td_name,
                            "repo_name": repo_name,
                            "message": error_msg,
                        }
                    )
                    failed_step = step_num
                    break

            # Upload this step's ATX artifacts and record the per-step S3 report.
            step_report_key = _upload_step_artifacts(step_num, pre_runs)
            validation = _step_validation_status(pre_runs)
            outcome, outcome_msg = step_outcome(changed, validation, build_command, repo_branch)
            outcome, outcome_msg = scope_outcome(outcome, outcome_msg, changed_paths(repo_dir, pre_head), step_scope)

            logger.info("Step %d: %s (%s)", step_num, outcome.upper(), outcome_msg)
            update_plan_step_status(step_num, outcome, outcome_msg, s3_report=step_report_key)
            steps_results.append(
                {"step_number": step_num, "status": outcome, "td_name": td_name,
                 "repo_name": repo_name, "s3_report": step_report_key, "message": outcome_msg,
                 "validation": validation, "branch": repo_branch}
            )

        # Upload ATX output to S3
        s3_prefix = f"applications/{APP_ID}/execution/transformation/{PLAN_ID}"

        # Upload ATXDocumentation if present
        for doc_dir in workspace.rglob("ATXDocumentation"):
            if doc_dir.is_dir():
                parent = doc_dir.relative_to(workspace).parent
                for f in doc_dir.rglob("*"):
                    if f.is_file():
                        key = f"{s3_prefix}/{parent}/ATXDocumentation/{f.relative_to(doc_dir)}"
                        s3_client.upload_file(str(f), ATX_S3_OUTPUT_BUCKET, key)

        # Upload ATX artifacts (worklog, git_instructions, validation_summary)
        atx_custom_dir = Path.home() / ".aws" / "atx" / "custom"
        if atx_custom_dir.exists():
            artifact_files = [
                "artifacts/worklog.log",
                "artifacts/git_instructions.md",
                "artifacts/validation_summary.md",
            ]
            for run_dir in sorted(atx_custom_dir.iterdir())[-4:]:  # last 4 runs (2 steps × 2)
                if run_dir.is_dir():
                    run_id = run_dir.name
                    for artifact_name in artifact_files:
                        artifact_path = run_dir / artifact_name
                        if artifact_path.exists():
                            key = f"{s3_prefix}/artifacts/{run_id}/{artifact_name.split('/')[-1]}"
                            s3_client.upload_file(str(artifact_path), ATX_S3_OUTPUT_BUCKET, key)
                            logger.info("Uploaded artifact: %s", key)

        logger.info("Workbench artifacts uploaded to s3://%s/%s/", ATX_S3_OUTPUT_BUCKET, s3_prefix)

        # Bedrock synthesis
        atx_output = {}
        try:
            resp = s3_client.list_objects_v2(Bucket=ATX_S3_OUTPUT_BUCKET, Prefix=f"{s3_prefix}/", MaxKeys=200)
            atx_output = {
                "s3_prefix": f"s3://{ATX_S3_OUTPUT_BUCKET}/{s3_prefix}/",
                "file_count": len(resp.get("Contents", [])),
            }
        except Exception as _err:
            logger.debug("best-effort step ignored: %s", _err)

        logger.info("Calling Bedrock workbench synthesis...")
        synthesis = call_workbench_synthesis(steps_results, atx_output)
        logger.info("Synthesis: status=%s, confidence=%s", synthesis.get("status"), synthesis.get("confidence"))

        # Finalize
        elapsed = time.time() - start_time
        s3_uri = f"s3://{ATX_S3_OUTPUT_BUCKET}/{s3_prefix}/"
        outcome_summary: dict[str, int] = {}
        for sr in steps_results:
            outcome_summary[sr["status"]] = outcome_summary.get(sr["status"], 0) + 1
        final_status = plan_final_status(failed_step, synthesis)
        if failed_step > 0:
            update_plan_status(
                "failed", failed_step=failed_step, synthesis=synthesis, error_message=f"Step {failed_step} failed"
            )
            logger.info("Workbench FAILED at step %d in %.1fs", failed_step, elapsed)
            _callback_failure("TransformStepFailed", f"Transformation step {failed_step} failed")
            sys.exit(1)
        else:
            # A failed synthesis (e.g. Bedrock AccessDenied) must not read as a clean
            # "completed" plan: the steps ran, the report did not.
            update_plan_status(
                final_status,
                completed_at=datetime.now(UTC).isoformat(),
                synthesis=synthesis,
                s3_prefix=s3_uri,
                outcome_summary=outcome_summary,
            )
            logger.info("Workbench %s in %.1fs — %d steps %s", final_status.upper(), elapsed, len(steps), outcome_summary)
            _callback_success({"status": final_status, "s3_prefix": s3_uri, "steps": len(steps),
                               "outcome_summary": outcome_summary})

    except Exception as exc:
        elapsed = time.time() - start_time
        logger.error("Workbench FAILED after %.1fs: %s", elapsed, exc, exc_info=True)
        update_plan_status("failed", error_message=str(exc))
        _callback_failure("CodeTransformationFailed", str(exc))
        sys.exit(1)


def _callback_success(payload: dict) -> None:
    """Resume the SFN workflow when transformation finishes (Option C callback)."""
    from backend.src.worker.config import job_id as _job_id
    from backend.src.worker.sfn_callback import finalize_job_once, resolve_token, send_success

    job_id = _job_id()
    token = resolve_token(job_id)
    if finalize_job_once(job_id, "COMPLETED", {"s3_prefix": payload.get("s3_prefix", "")}):
        send_success(token, payload)


def _callback_failure(error: str, cause: str) -> None:
    from backend.src.worker.config import job_id as _job_id
    from backend.src.worker.sfn_callback import finalize_job_once, resolve_token, send_failure

    job_id = _job_id()
    token = resolve_token(job_id)
    if finalize_job_once(job_id, "FAILED", {"error_msg": cause[:500]}):
        send_failure(token, error, cause)
