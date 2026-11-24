#!/usr/bin/env python3
"""Golden-app end-to-end regression for AMF.

Drives each golden app (scripts/golden_apps.json) through the real journey on a
deployed AMF — register → Discovery → Assessment → Design → Execution → Deploy →
Validation — approving every gate with the app's canned inputs, and fails unless
Validation's verdict is pass / pass_with_warnings.

Run against a local API (scripts/start.sh, DEV_AUTH=true) pointed at the UAT data plane.
--api defaults to $AMF_API_URL, else http://localhost:8000/api/v1 (start.sh's port). The
assessment gate's target_account comes from --target-account / $GOLDEN_TARGET_ACCOUNT, else
the caller's account (STS):

    backend/.venv/bin/python scripts/e2e_golden.py --apps APP-MUSIC,APP-MOVIES
    backend/.venv/bin/python scripts/e2e_golden.py --token "$JWT" --api https://<portal>/api/v1
    backend/.venv/bin/python scripts/e2e_golden.py --apps-file scripts/test_apps.json --apps all --register-only

Exit code: 0 when every app validated, 1 otherwise. Validation rows are printed
from the gate's decision summary, and from DynamoDB when --table is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GOLDEN = Path(__file__).with_name("golden_apps.json")
# Wall-clock budget per gate wait (ATX analysis/transforms and terraform dominate).
PHASE_TIMEOUT_S = {"discovery_review": 900, "assessment_review": 3600, "design_review": 1800,
                   "execution_review": 5400, "deploy_review": 5400, "validation_review": 3600}
POLL_S = 30
GET_RETRIES = 8
HTTP_TIMEOUT_S = 60
DEV_LOGIN_TIMEOUT_S = 30
OK_VERDICTS = ("pass", "pass_with_warnings")


def log(app: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [{app}] {msg}", flush=True)


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _http_base(base: str) -> str:
    """The AMF API base URL: https, or plain http only to a loopback host (start.sh). No file:/ftp:, no
    userinfo, and the bearer token never crosses the network in clear text."""
    parts = urllib.parse.urlsplit(base)
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        raise SystemExit(f"--api must be an http(s) URL with a host, got {base!r}")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK_HOSTS):
        raise SystemExit(f"--api must be https (http only for localhost), got {base!r}")
    return base.rstrip("/")


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = _http_base(base), token

    def call(self, method: str, path: str, body=None) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}", method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            # self.base passed _http_base(): https, or http to loopback only; path is our own API route.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:  # noqa: S310  # nosec B310 — scheme/host checked by _http_base
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return {"status": "error", "http_status": e.code, "error": e.read().decode()[:500]}

    def ok(self, method: str, path: str, body=None) -> dict:
        # GETs are idempotent: ride out flaky networks / transient 5xx (a laptop sleep once
        # surfaced as "Signature expired"). Approvals (POST) are never retried.
        attempts = GET_RETRIES if method == "GET" else 1
        for i in range(attempts):
            try:
                out = self.call(method, path, body)
            except (TimeoutError, OSError) as e:  # URLError is an OSError
                out = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            transient = out.get("status") == "error" and (not out.get("http_status") or out["http_status"] >= 500)
            if out.get("status") != "error" and not out.get("http_status"):
                return out
            if not transient or i == attempts - 1:
                raise RuntimeError(f"{method} {path} failed: {json.dumps(out)[:500]}")
            time.sleep(POLL_S)
        return out


def dev_token(base: str) -> str:
    # _http_base(): https, or http to loopback only.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(f"{_http_base(base)}/auth/dev-login", timeout=DEV_LOGIN_TIMEOUT_S) as r:  # noqa: S310  # nosec B310 — scheme/host checked
        return json.loads(r.read())["access_token"]


def ensure_app(api: Api, app_id: str, spec: dict) -> None:
    existing = api.call("GET", f"/portfolio/apps/{app_id}")
    if existing.get("status") == "success" and (existing.get("data") or {}).get("app_id"):
        log(app_id, "registered already — refreshing golden fields")
    else:
        api.ok("POST", "/portfolio/apps", {"app_id": app_id, **spec["create"]})
        log(app_id, "registered")
    api.ok("PUT", f"/portfolio/apps/{app_id}", spec["put"])


def workflow(api: Api, app_id: str) -> dict:
    data = api.ok("GET", "/workflow/active").get("data") or []
    items = data if isinstance(data, list) else data.get("workflows", [])
    return next((w for w in items if w.get("app_id") == app_id), {})


def approve(api: Api, app_id: str, step: str, spec: dict, summary: dict) -> None:
    inputs = None
    if step == "discovery_review":
        api.ok("PUT", f"/portfolio/apps/{app_id}", {"migration_strategy": spec["discovery_strategy"]})
    elif step == "assessment_review":
        inputs = dict(spec["assessment_inputs"])
        # Confirm the detected wiring exactly as the approval modal does.
        api.ok("PUT", f"/portfolio/apps/{app_id}", inputs)
    elif step in ("execution_review", "deploy_review"):
        head = summary.get("head_commit")
        inputs = {"deploy_commit": head} if head else None
        if not head:
            log(app_id, "WARNING: gate summary has no head_commit — approving without a pinned commit")
    body = {"app_id": app_id, "step": step, "approved": True, "comment": "golden e2e (scripts/e2e_golden.py)"}
    if inputs is not None:
        body["inputs"] = inputs
    api.ok("POST", "/workflow/approve", body)
    log(app_id, f"approved {step}")


def validation_rows(app_id: str, table_name: str, region: str) -> tuple[str, list]:
    import boto3
    from boto3.dynamodb.conditions import Key
    t = boto3.resource("dynamodb", region_name=region).Table(table_name)
    items = t.query(KeyConditionExpression=Key("PK").eq(f"APP#{app_id}")
                    & Key("SK").begins_with("ANALYSIS#validation-results#")).get("Items", [])
    if not items:
        return "missing", []
    res = sorted(items, key=lambda i: i["SK"])[-1].get("result") or {}
    return str(res.get("verdict", "unknown")), list(res.get("checks") or [])


def run_app(api: Api, app_id: str, spec: dict, args) -> bool:
    ensure_app(api, app_id, spec)
    wf = workflow(api, app_id)
    attach = False
    if wf.get("status") == "RUNNING":
        if args.attach:
            attach = True
            log(app_id, "attaching to the running workflow")
        elif not args.restart:
            log(app_id, "a workflow is already RUNNING — pass --restart to cancel it or --attach to follow it")
            return False
        else:
            api.ok("POST", "/workflow/cancel", {"app_id": app_id})
            log(app_id, "cancelled the running workflow")
            time.sleep(5)
    if not attach:
        started = api.ok("POST", "/workflow/start", {"app_id": app_id})
        log(app_id, f"started {(started.get('data') or {}).get('execution_arn', '')}")

    handled: set[str] = set()
    waiting_since, last_step = time.time(), ""
    while True:
        wf = workflow(api, app_id)
        status, step = wf.get("status", ""), wf.get("current_step", "")
        if step != last_step:
            log(app_id, f"{status} step={step}")
            last_step, waiting_since = step, time.time()
        if status in ("FAILED", "CANCELLED", "COMPLETED") and "validation_review" not in handled:
            log(app_id, f"workflow ended {status} at {step} before validation")
            return False
        pending = {p.get("step"): p for p in wf.get("pending") or []}
        if "validation_review" in pending:
            summary = pending["validation_review"].get("decision_summary") or {}
            log(app_id, f"validation gate: {summary.get('what_changed', '')}")
            verdict, rows = ("", [])
            if args.table:
                verdict, rows = validation_rows(app_id, args.table, args.region)
            else:
                verdict = next((v for v in OK_VERDICTS + ("fail",) if f"verdict={v}" in str(summary)), "unknown")
            for r in rows:
                print(f"    {r.get('result', ''):<13} {r.get('check', ''):<28} {str(r.get('detail', ''))[:110]}")
            log(app_id, f"VERDICT {verdict}")
            if args.teardown:
                api.call("POST", "/workflow/cancel", {"app_id": app_id})
                log(app_id, "workflow cancelled at validation_review (infra teardown: use the Deploy gate's "
                            "Tear down action)")
            return verdict in OK_VERDICTS
        for s, p in pending.items():
            if s and s not in handled:
                approve(api, app_id, s, spec, p.get("decision_summary") or {})
                handled.add(s)
        budget = PHASE_TIMEOUT_S.get(step, 3600)
        if time.time() - waiting_since > budget:
            log(app_id, f"TIMEOUT: still at {step} after {budget}s")
            return False
        time.sleep(POLL_S)


def caller_account(region: str) -> str:
    """The STS caller account — the default deploy target (DEPLOY_ALLOWED_ACCOUNTS defaults to it)."""
    import boto3
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", default=os.environ.get("AMF_API_URL", "http://localhost:8000/api/v1"),
                    help="AMF API base (default: $AMF_API_URL, else start.sh's http://localhost:8000/api/v1)")
    ap.add_argument("--token-from", choices=["dev-login"], default="dev-login",
                    help="obtain a token from the API's DEV_AUTH login (local dev only)")
    ap.add_argument("--token", default="", help="bearer token (Cognito JWT) — overrides --token-from")
    ap.add_argument("--apps", default="APP-MUSIC,APP-MOVIES", help="comma-separated app ids, or 'all'")
    ap.add_argument("--register-only", action="store_true",
                    help="create/update the apps (and link their repos) without starting any workflow")
    ap.add_argument("--restart", action="store_true", help="cancel an already-running workflow first")
    ap.add_argument("--attach", action="store_true", help="follow an already-running workflow instead of starting one")
    ap.add_argument("--teardown", action="store_true", help="cancel the workflow once validation finished")
    ap.add_argument("--table", default="", help="DynamoDB table to read full validation rows from (e.g. uat_portfolio)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--target-account", default=os.environ.get("GOLDEN_TARGET_ACCOUNT", ""),
                    help="12-digit deploy account for the assessment gate "
                         "(default: $GOLDEN_TARGET_ACCOUNT, else the STS caller account)")
    ap.add_argument("--apps-file", default=str(GOLDEN), help="golden app definitions (default scripts/golden_apps.json)")
    args = ap.parse_args()

    golden = json.loads(Path(args.apps_file).read_text())["apps"]
    target_account = args.target_account or caller_account(args.region)
    for spec in golden.values():
        if "assessment_inputs" in spec:
            spec["assessment_inputs"].setdefault("target_account", target_account)
    api = Api(args.api, args.token or dev_token(args.api))
    results = {}
    wanted = list(golden) if args.apps.strip() == "all" else [a.strip() for a in args.apps.split(",") if a.strip()]
    for app_id in wanted:
        if app_id not in golden:
            print(f"unknown golden app {app_id} (have: {', '.join(golden)})")
            return 2
        try:
            if args.register_only:
                ensure_app(api, app_id, golden[app_id])
                log(app_id, f"registered ({golden[app_id].get('discovery_strategy', '?')})")
                results[app_id] = True
                continue
            results[app_id] = run_app(api, app_id, golden[app_id], args)
        except Exception as e:  # noqa: BLE001 — report and move to the next app
            log(app_id, f"ERROR {type(e).__name__}: {e}")
            results[app_id] = False
    print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'}  {a}" for a, ok in results.items()))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
