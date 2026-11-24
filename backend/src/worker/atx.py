"""
ATX execution — clone repos, run ATX CLI, upload results to S3.
Shared between assessment (analysis) and workbench (transformation) modes.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
from pathlib import Path

from backend.src.config import settings
from backend.src.worker.argsafe import confined, safe_clone_url, safe_dir_name, safe_ref, safe_td_name
from backend.src.worker.config import (
    ATX_S3_OUTPUT_BUCKET,
    ATX_SHELL_TIMEOUT,
    ATX_TD_NAME,
    AWS_REGION,
    GIT_USER_EMAIL,
    GIT_USER_NAME,
    logger,
    s3_client,
)
from backend.src.worker.gitcmd import run_git
from backend.src.worker.sandbox import prepare_build_tree
from backend.src.worker.scope import atx_path


def repo_provider(url: str) -> str:
    """Classify a repo URL by hosting provider so we pick the right git auth."""
    u = (url or "").lower()
    if "git-codecommit" in u or u.startswith("codecommit::"):
        return "codecommit"
    return "other"


def configure_git():
    """Configure git globally for the container. No secret is written: AMF sources AWS CodeCommit only,
    authenticated by the ECS task role through the AWS credential helper."""
    run_git(["config", "--global", "user.email", GIT_USER_EMAIL], check=False, capture_output=True)
    run_git(["config", "--global", "user.name", GIT_USER_NAME], check=False, capture_output=True)
    run_git(["config", "--global", "init.defaultBranch", "main"], check=False, capture_output=True)
    # CodeCommit git-over-HTTPS via the AWS credential helper (uses the ECS task
    # role), scoped to the CodeCommit host.
    cc_host = f"https://git-codecommit.{AWS_REGION}.amazonaws.com"
    run_git(
        ["config", "--global", f"credential.{cc_host}.helper", "!aws codecommit credential-helper $@"],
        check=False,
        capture_output=True,
    )
    run_git(
        ["config", "--global", f"credential.{cc_host}.UseHttpPath", "true"],
        check=False,
        capture_output=True,
    )


def clone_repo(url: str, branch: str, target_dir: Path) -> bool:
    """Clone a repo. Returns True on success."""
    try:
        url, branch = safe_clone_url(url), safe_ref(branch, "branch")
    except ValueError as exc:
        logger.error("Clone refused for %s: %s", target_dir.name, exc)
        return False

    repo_name = target_dir.name
    logger.info("Cloning %s (branch: %s)", repo_name, branch)

    result = run_git(
        ["clone", "--depth", "1", "--branch", branch, "--", url, str(target_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Clone failed for %s (branch: %s): %s", repo_name, branch, result.stderr.strip())
        result = run_git(
            ["clone", "--depth", "1", "--", url, str(target_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Clone failed for %s (no branch): %s", repo_name, result.stderr.strip())
            return False

    prepare_build_tree(target_dir)  # shared with the build user, .git kept private (R9)
    logger.info("Cloned %s successfully", repo_name)
    return True


def clone_repo_full(
    url: str,
    branch: str,
    target_dir: Path,
    source_ref: str = "",
    review_base_branch: str = "",
) -> bool:
    """Clone a repo with full history for transformation and push.

    The normal path requires ``branch`` to exist remotely. For a demo or a new
    migration, a repo record may explicitly supply ``source_ref`` (a commit or
    existing branch). If the requested branch is absent, clone the default
    branch and create the target branch from that exact ref. This lets the
    factory produce a reviewable modernization branch without rewriting main.
    """
    if not _clone_full(url, branch, target_dir, source_ref, review_base_branch):
        return False
    prepare_build_tree(target_dir)  # shared with the build user, .git kept private (R9)
    return True


def _clone_full(url: str, branch: str, target_dir: Path, source_ref: str, review_base_branch: str) -> bool:
    try:
        url, branch = safe_clone_url(url), safe_ref(branch, "branch")
        source_ref = safe_ref(source_ref, "source_ref") if source_ref else ""
        review_base_branch = safe_ref(review_base_branch, "review base branch") if review_base_branch else ""
    except ValueError as exc:
        logger.error("Full clone refused for %s: %s", target_dir.name, exc)
        return False
    result = run_git(
        ["clone", "--branch", branch, "--", url, str(target_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info("Cloned %s (full history, branch %s)", target_dir.name, branch)
        return True

    if not source_ref:
        logger.error("Full clone failed for %s: %s", target_dir.name, result.stderr.strip())
        return False

    logger.info(
        "Branch %s is absent; creating it from explicit source_ref %s",
        branch, source_ref,
    )
    result = run_git(["clone", "--", url, str(target_dir)], capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Full fallback clone failed for %s: %s", target_dir.name, result.stderr.strip())
        return False
    if review_base_branch:
        result = run_git(
            ["push", "origin", f"{source_ref}:refs/heads/{review_base_branch}"],
            cwd=target_dir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error(
                "Could not publish review base %s from %s: %s",
                review_base_branch, source_ref, result.stderr.strip(),
            )
            return False
        logger.info("Published review base branch %s from %s", review_base_branch, source_ref)

    # A DERIVED ref (origin/<recorded branch>) can be stale — APP-TE 2026-10-13: L2 said
    # `master`, the repo only has `main`. Fall back to the clone's default branch.
    # An explicit source_ref (exact commit) must still match exactly.
    if source_ref.startswith("origin/") and run_git(
        ["rev-parse", "--verify", "--quiet", f"{source_ref}^{{commit}}"],
        cwd=target_dir, capture_output=True, text=True,
    ).returncode != 0:
        logger.warning("Source ref %s not found in %s; using the repository default branch",
                       source_ref, target_dir.name)
        source_ref = "HEAD"

    result = run_git(
        ["checkout", "-b", branch, source_ref],
        cwd=target_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(
            "Could not create branch %s from %s: %s",
            branch, source_ref, result.stderr.strip(),
        )
        return False

    logger.info("Created local branch %s from %s", branch, source_ref)
    return True


def run_atx_analysis(app_id: str, repos: list[dict], build_dir: str = "", primary_repo: str = "") -> dict:
    """Run ATX code analysis on all repos. Returns ATX output dict."""
    workspace = Path("/atx-workspace")
    workspace.mkdir(exist_ok=True)

    configure_git()

    # Clone repos
    for r in repos:
        url = str(r.get("url") or "").strip()
        if not url:
            continue
        branch = str(r.get("branch") or "main").strip()
        try:  # the dir name comes from the URL: one plain component, inside the workspace
            repo_dir = confined(workspace, safe_dir_name(url.rstrip("/").split("/")[-1].replace(".git", "")))
        except ValueError as exc:
            logger.error("Repo %s skipped: %s", url[:120], exc)
            continue
        clone_repo(url, branch, repo_dir)

    repos_found = [d for d in workspace.iterdir() if d.is_dir() and d.name != "atx-run.log"]
    if not repos_found:
        logger.warning("No repos cloned — nothing to analyze")
        return {"status": "no_repos", "message": "No repositories were cloned successfully", "file_count": 0}

    logger.info("Running ATX on %d repo(s): %s", len(repos_found), [d.name for d in repos_found])

    # ATX requires git repo at workspace root
    run_git(["init"], cwd=workspace, capture_output=True, check=False)
    run_git(["add", "."], cwd=workspace, capture_output=True, check=False)
    run_git(
        ["commit", "--allow-empty", "-m", "atx workspace init"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )

    # Check ATX availability
    atx_bin = shutil.which("atx")
    if not atx_bin:
        logger.warning("ATX binary not found in PATH")
        return {"status": "atx_not_available", "message": "ATX CLI not installed", "file_count": 0}

    # Run ATX at the workspace root, scoped to the app's build_dir when the app is
    # one folder of a single (mono)repo (spec 12). With several repos the
    # analysis covers all of them, as before.
    target = "."
    if build_dir and len(repos_found) == 1 and (not primary_repo or repos_found[0].name == primary_repo):
        target = atx_path(workspace, f"{repos_found[0].name}/{build_dir}")
    elif build_dir:
        logger.info("build_dir %r not applied: %d repos analysed together", build_dir, len(repos_found))
    # List argv (no shell, no re-splitting): the TD name is validated, `target` is confined to the workspace.
    cmd = ["atx", "custom", "def", "exec", "-n", safe_td_name(ATX_TD_NAME), "-p", target, "-x", "-t",
           "--do-not-learn"]
    logger.info("[DEBUG] ATX command: %s", cmd)
    logger.info("[DEBUG] ATX working directory: %s", str(workspace))

    result = subprocess.run(  # nosec B603 B607 — list argv; validated TD name; confined -p path; timeout
        cmd,
        shell=False,
        cwd=str(workspace),
        timeout=ATX_SHELL_TIMEOUT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.stdout:
        for line in result.stdout.splitlines():
            logger.info("[ATX] %s", line)
    logger.info("ATX exit code: %d", result.returncode)

    # Discover ATX run ID
    atx_log = workspace / "atx-run.log"
    if result.stdout:
        with open(atx_log, "w") as f:
            f.write(result.stdout)

    atx_custom_dir = Path.home() / ".aws" / "atx" / "custom"
    atx_run_id = ""

    if atx_custom_dir.exists():
        runs = sorted(atx_custom_dir.iterdir())
        if runs:
            atx_run_id = runs[-1].name

    if not atx_run_id and atx_log.exists():
        match = re.search(r"\d{8}_\d{6}_[a-f0-9]+", atx_log.read_text())
        if match:
            atx_run_id = match.group()

    if not atx_run_id:
        logger.warning("Could not discover ATX run ID")
        return {"status": "no_output", "message": "ATX did not produce a run ID", "file_count": 0}

    logger.info("ATX run ID: %s", atx_run_id)

    # Upload to S3 — prefix based on transformation type
    td_prefix = _td_to_s3_prefix(ATX_TD_NAME)
    s3_prefix = f"applications/{app_id}/analysis/{td_prefix}/{atx_run_id}"
    _upload_atx_output(workspace, s3_prefix)

    # Read key documents
    return _fetch_atx_output(app_id, atx_run_id, td_prefix)


def _td_to_s3_prefix(td_name: str) -> str:
    """Convert ATX transformation definition name to S3 prefix.

    AWS/comprehensive-codebase-analysis → comprehensive-codebase-analysis
    AWS/modernization-readiness-analysis → modernization-readiness-analysis
    AWS/agentic-readiness-analysis → agentic-readiness-analysis
    AWS/java-performance-optimization → java-performance-optimization
    """
    return td_name.replace("AWS/", "")


def _upload_atx_output(workspace: Path, s3_prefix: str):
    """Upload ATXDocumentation, modernization reports, and logs to S3."""
    uploaded = 0

    # Upload ATXDocumentation directories (comprehensive-codebase-analysis output)
    for doc_dir in workspace.rglob("ATXDocumentation"):
        if doc_dir.is_dir():
            relative_to_workspace = doc_dir.relative_to(workspace)
            for f in doc_dir.rglob("*"):
                if f.is_file():
                    relative_path = f.relative_to(doc_dir)
                    parent = relative_to_workspace.parent
                    if parent != Path("."):
                        key = f"{s3_prefix}/{parent}/ATXDocumentation/{relative_path}"
                    else:
                        key = f"{s3_prefix}/ATXDocumentation/{relative_path}"
                    s3_client.upload_file(str(f), ATX_S3_OUTPUT_BUCKET, key)
                    uploaded += 1

    # Upload modernization-readiness-analysis output (mod reports)
    for mod_dir in workspace.rglob("modernization-readiness-analysis"):
        if mod_dir.is_dir():
            relative_to_workspace = mod_dir.relative_to(workspace)
            for f in mod_dir.rglob("*"):
                if f.is_file():
                    key = f"{s3_prefix}/{relative_to_workspace}/{f.name}"
                    s3_client.upload_file(str(f), ATX_S3_OUTPUT_BUCKET, key)
                    uploaded += 1

    # Upload any other report files (agentic-readiness, java-performance, etc.)
    for pattern in ["*-report.md", "*-report.json", "*-report.html", "*-report.metadata.json"]:
        for f in workspace.rglob(pattern):
            if f.is_file():
                relative = f.relative_to(workspace)
                key = f"{s3_prefix}/{relative}"
                # Avoid duplicates from already-uploaded dirs
                try:
                    s3_client.upload_file(str(f), ATX_S3_OUTPUT_BUCKET, key)
                    uploaded += 1
                except Exception as _err:
                    logger.debug("best-effort step ignored: %s", _err)

    # Upload ATX log
    atx_log = workspace / "atx-run.log"
    if atx_log.exists():
        s3_client.upload_file(str(atx_log), ATX_S3_OUTPUT_BUCKET, f"{s3_prefix}/atx-run.log")
        uploaded += 1

    logger.info("ATX output uploaded to s3://%s/%s/ (%d files)", ATX_S3_OUTPUT_BUCKET, s3_prefix, uploaded)


def _fetch_atx_output(app_id: str, atx_run_id: str, td_prefix: str = "comprehensive-codebase-analysis") -> dict:
    """Read ATX output from S3 for synthesis."""
    # Primary prefix: applications/{app_id}/analysis/{td_prefix}/{run_id}/
    run_prefix = f"applications/{app_id}/analysis/{td_prefix}/{atx_run_id}/"
    resp = s3_client.list_objects_v2(Bucket=ATX_S3_OUTPUT_BUCKET, Prefix=run_prefix, MaxKeys=500)
    objects = resp.get("Contents", [])

    # Backward compatibility: try old prefix if nothing found
    if not objects:
        old_prefix = f"{td_prefix}/{app_id}/{atx_run_id}/"
        resp = s3_client.list_objects_v2(Bucket=ATX_S3_OUTPUT_BUCKET, Prefix=old_prefix, MaxKeys=500)
        objects = resp.get("Contents", [])
        if objects:
            run_prefix = old_prefix
            logger.info("Using legacy S3 prefix: %s", old_prefix)

    if not objects:
        old_prefix2 = f"atx-code-analysis/{app_id}/{atx_run_id}/"
        resp = s3_client.list_objects_v2(Bucket=ATX_S3_OUTPUT_BUCKET, Prefix=old_prefix2, MaxKeys=500)
        objects = resp.get("Contents", [])
        if objects:
            run_prefix = old_prefix2
            logger.info("Using legacy S3 prefix: %s", old_prefix2)

    output = {
        "atx_run_id": atx_run_id,
        "s3_prefix": f"s3://{ATX_S3_OUTPUT_BUCKET}/{run_prefix}",
        "file_count": len(objects),
        "files": [o["Key"].replace(run_prefix, "") for o in objects],
    }

    key_docs = {
        "ATXDocumentation/README.md": "readme",
        "ATXDocumentation/technical-debt-report.md": "technical_debt_report",
        "ATXDocumentation/project-overview.md": "project_overview",
        "ATXDocumentation/analysis/security-patterns.md": "security_analysis",
        "ATXDocumentation/analysis/dependency-analysis.md": "dependency_analysis",
        "ATXDocumentation/analysis/complexity-analysis.md": "complexity_analysis",
        "ATXDocumentation/analysis/code-metrics.md": "code_metrics",
        "ATXDocumentation/analysis/tech-debt.md": "tech_debt_analysis",
        "ATXDocumentation/architecture/system-overview.md": "architecture_overview",
        "ATXDocumentation/architecture/components.md": "architecture_components",
        "ATXDocumentation/architecture/dependencies.md": "architecture_dependencies",
        "ATXDocumentation/architecture/patterns.md": "architecture_patterns",
        "ATXDocumentation/diagrams/architecture/system-context.md": "diagrams_architecture",
        "ATXDocumentation/diagrams/structural/component-diagrams.md": "diagrams_components",
        "ATXDocumentation/diagrams/structural/component-diagram.md": "diagrams_components",
        "ATXDocumentation/diagrams/behavioral/sequence-diagrams.md": "diagrams_behavioral",
        "ATXDocumentation/behavior/business-logic.md": "behavior_business_logic",
        "ATXDocumentation/behavior/decision-logic.md": "behavior_decision_logic",
        "ATXDocumentation/behavior/error-handling.md": "behavior_error_handling",
        "ATXDocumentation/behavior/workflows.md": "behavior_workflows",
        "ATXDocumentation/migration/component-order.md": "migration_order",
        "ATXDocumentation/migration/test-specifications.md": "migration_tests",
        "ATXDocumentation/migration/validation-criteria.md": "migration_validation",
        "ATXDocumentation/technical-debt/remediation-plan.md": "remediation_plan",
        "ATXDocumentation/technical-debt/outdated-components.md": "outdated_components",
        "ATXDocumentation/technical-debt/maintenance-burden.md": "tech_debt_maintenance",
        "ATXDocumentation/technical-debt/summary.md": "tech_debt_summary",
        "ATXDocumentation/reference/api-reference.md": "api_reference",
        "ATXDocumentation/reference/data-models.md": "ref_data_models",
        "ATXDocumentation/reference/interfaces.md": "ref_interfaces",
        "ATXDocumentation/reference/modules.md": "ref_modules",
        "ATXDocumentation/reference/program-structure.md": "ref_program_structure",
        "ATXDocumentation/specialized/infrastructure-deployment.md": "infrastructure_deployment",
        "ATXDocumentation/specialized/infrastructure.md": "infrastructure_deployment",
        "ATXDocumentation/specialized/api-documentation.md": "specialized_api_docs",
    }

    for doc_path, field_name in key_docs.items():
        full_key = f"{run_prefix}{doc_path}"
        # Also check under repo subdirectories (e.g., repo-name/ATXDocumentation/...)
        matching_key = None
        if any(o["Key"] == full_key for o in objects):
            matching_key = full_key
        else:
            # Search for the doc under any repo subdirectory
            suffix = doc_path  # e.g., "ATXDocumentation/project-overview.md"
            for o in objects:
                if o["Key"].endswith(suffix):
                    matching_key = o["Key"]
                    break

        if matching_key:
            try:
                obj = s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=matching_key)
                body = obj["Body"].read().decode("utf-8")
                output[field_name] = body[:settings.atx_report_max_chars]
            except Exception as exc:
                logger.warning("Failed to read ATX doc %s: %s", matching_key, exc)

    # Also look for modernization/agentic readiness reports
    for o in objects:
        key = o["Key"]
        if key.endswith("-mod-report.md"):
            try:
                obj = s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)
                body = obj["Body"].read().decode("utf-8")
                output["modernization_report"] = body[:settings.atx_report_max_chars]
            except Exception as exc:
                logger.warning("Failed to read mod report %s: %s", key, exc)
        elif key.endswith("-mod-report.json"):
            try:
                obj = s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)
                body = obj["Body"].read().decode("utf-8")
                output["modernization_report_json"] = body[:settings.atx_report_max_chars]
            except Exception as exc:
                logger.warning("Failed to read mod report JSON %s: %s", key, exc)
        elif key.endswith("-ara-report.md"):
            try:
                obj = s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)
                body = obj["Body"].read().decode("utf-8")
                output["agentic_report"] = body[:settings.atx_report_max_chars]
            except Exception as exc:
                logger.warning("Failed to read agentic report %s: %s", key, exc)
        elif key.endswith("-ara-report.json"):
            try:
                obj = s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)
                body = obj["Body"].read().decode("utf-8")
                output["agentic_report_json"] = body[:settings.atx_report_max_chars]
            except Exception as exc:
                logger.warning("Failed to read agentic report JSON %s: %s", key, exc)

    return output
