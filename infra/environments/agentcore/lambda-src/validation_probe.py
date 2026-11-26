"""Validation tiers L1–L3: requests that reach the deployed application.

L0 (platform) lives in tool_execution._static_checks/_live_checks. This module runs
only after L0 found the stack deployed, and appends rows to the same `checks` list:

  L1:<name>  smoke    — health path must answer 200; every known parameter-free GET
                        route must not 5xx.
  L2:<name>  contract — the repo's amf-smoke.json cases (else OpenAPI GET operations).
                        Write cases run only with META validation_write_tests=true.
  L3:<name>  data     — opt-in round-trip: a passing case with `verify_dynamodb` is
                        checked in the app's table, then the test record is deleted.
                        `verify_dynamodb.table` is ignored: only the app's own table,
                        keyed by its own key attributes, is ever touched.

Transport per target:
  • ecs_fargate / ec2 → the app's ALB is internal, so a short-lived probe Lambda
    (probe_handler.py) runs in the app VPC's private subnets; always deleted after.
  • lambda → the HTTP API endpoint, SigV4-signed from here (routes are AWS_IAM);
    JWT-protected APIs are skipped (no credentials to present).

Nothing here raises: every failure is a row with evidence. Verdict rules stay in
tool_execution (any `fail` row → validation fails).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PROBE_SOURCE = Path(__file__).with_name("probe_handler.py")
_SMOKE_FILE = "amf-smoke.json"
_OPENAPI_FILES = ("openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml", "swagger.yml")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_EXCERPT = 160
# Tunables (env vars set on the tool_execution Lambda in lambdas.tf; defaults = the values tuned live).
# Waiter polls x delay: 96 x 5 s = 8 min — a first VPC function in a NEW VPC can need several
# minutes for its ENI (APP-TE 2026-10-27: >3 min); fits the 900 s tool timeout.
_PROBE_ACTIVE_ATTEMPTS = int(os.environ.get("VALIDATION_PROBE_ACTIVE_ATTEMPTS", "96"))
_PROBE_ACTIVE_DELAY = int(os.environ.get("VALIDATION_PROBE_ACTIVE_DELAY_SECONDS", "5"))
# create_function retries while a just-created role is not yet assumable.
_PROBE_CREATE_ATTEMPTS = max(1, int(os.environ.get("VALIDATION_PROBE_CREATE_ATTEMPTS", "5")))
_PROBE_CREATE_RETRY_DELAY = float(os.environ.get("VALIDATION_PROBE_CREATE_RETRY_SECONDS", "5"))
_PROBE_RUNTIME = os.environ.get("VALIDATION_PROBE_RUNTIME", "python3.12")
_PROBE_TIMEOUT = int(os.environ.get("VALIDATION_PROBE_TIMEOUT_SECONDS", "120"))
_PROBE_MEMORY = int(os.environ.get("VALIDATION_PROBE_MEMORY_MB", "256"))
# boto3 read timeout for the probe's synchronous invoke — must exceed _PROBE_TIMEOUT.
_PROBE_INVOKE_READ_TIMEOUT = int(os.environ.get("VALIDATION_PROBE_INVOKE_READ_TIMEOUT_SECONDS", "150"))
_PROBE_INVOKE_MAX_ATTEMPTS = int(os.environ.get("VALIDATION_PROBE_INVOKE_MAX_ATTEMPTS", "1"))
# Per-request HTTP timeout for every L1/L2 case.
_REQUEST_TIMEOUT = int(os.environ.get("PROBE_REQUEST_TIMEOUT_SECONDS", "10"))
# Health path when neither META nor the repo scan recorded one (same env as tool_execution).
_DEFAULT_HEALTH_PATH = os.environ.get("DEFAULT_HEALTH_CHECK_PATH", "/health")


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "on") if v is not None else False


def _boto_client(name: str, region: str):
    import boto3
    from botocore.config import Config

    # The probe's synchronous invoke can run up to its 120 s timeout — beyond boto3's default
    # 60 s read timeout (APP-TE 2026-10-27: ReadTimeoutError on .../invocations).
    cfg = (Config(read_timeout=_PROBE_INVOKE_READ_TIMEOUT, retries={"max_attempts": _PROBE_INVOKE_MAX_ATTEMPTS})
           if name == "lambda" else None)
    return boto3.client(name, region_name=region, config=cfg)


# ── case assembly ──────────────────────────────────────────────────────────────

def _health_path(meta: dict) -> str:
    p = str(meta.get("health_check_path") or meta.get("detected_health_check_path") or _DEFAULT_HEALTH_PATH)
    return p if p.startswith("/") else "/" + p


def _smoke_cases(meta: dict) -> list[dict]:
    """L1: the health path (expect 200) + every known parameter-free GET route (expect < 500)."""
    health = _health_path(meta)
    if (not meta.get("health_check_path") and not meta.get("detected_health_check_path")
            and str(meta.get("target_compute") or "").lower() == "lambda"):
        # An API of several functions has no /health route: its first parameter-free GET is the health check.
        health = next((m.group(2) for fn in meta.get("detected_lambda_functions") or [] if isinstance(fn, dict)
                       for r in fn.get("routes") or []
                       if (m := re.fullmatch(r"\s*(GET|ANY)\s+(/[^{\s]*)\s*", str(r), re.I))), health)
    cases = [{"name": "health", "method": "GET", "path": health, "expect_status": 200}]
    seen = {health}
    for fn in meta.get("detected_lambda_functions") or []:
        for route in (fn.get("routes") or []) if isinstance(fn, dict) else []:
            m = re.fullmatch(r"\s*(GET|ANY)\s+(/\S*)\s*", str(route), re.I)
            if m and "{" not in m.group(2) and m.group(2) not in seen:
                seen.add(m.group(2))
                cases.append({"name": f"GET {m.group(2)}", "method": "GET", "path": m.group(2), "expect_lt": 500})
    return cases


def _repo_file(codecommit, repo: str, branches: list[str], paths: list[str]) -> tuple[str, str]:
    """(text, 'path@branch') of the first file found, else ('', '')."""
    for b in branches:
        for p in paths:
            try:
                blob = codecommit.get_file(repositoryName=repo, filePath=p, commitSpecifier=b)["fileContent"]
                return (blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)), f"{p}@{b}"
            except Exception as _err:  # noqa: BLE001 — not found on this branch/path
                logger.debug("best-effort step skipped: %s", _err)
                continue
    return "", ""


def _repo_name(meta: dict, table, app_id: str) -> str:
    """The app's CodeCommit repo name (REPO# sibling with a CodeCommit URL)."""
    try:
        from boto3.dynamodb.conditions import Key
        items = table.query(KeyConditionExpression=Key("PK").eq(f"APP#{app_id}")
                            & Key("SK").begins_with("REPO#")).get("Items", [])
    except Exception:  # noqa: BLE001
        items = []
    for r in items:
        url = str(r.get("url") or "")
        if "codecommit" in url or str(r.get("provider", "")).lower() == "codecommit":
            return url.rstrip("/").split("/")[-1].removesuffix(".git") or str(r.get("name") or "")
    return str(meta.get("repo_name") or "")


def _contract_cases(app_id: str, meta: dict, table, codecommit) -> tuple[list[dict], str]:
    """L2 cases from amf-smoke.json (else OpenAPI GETs) + where they came from."""
    repo = _repo_name(meta, table, app_id)
    if not repo or codecommit is None:
        return [], ""
    # The migration branch can be cut from a linked feature branch (REPO# branch) that predates
    # a contract added on the default branch — fall back to it (APP-CLI 2026-10-23).
    try:
        default = str(codecommit.get_repository(repositoryName=repo)["repositoryMetadata"].get("defaultBranch") or "")
    except Exception:  # noqa: BLE001
        default = ""
    branches = [b for b in dict.fromkeys([str(meta.get("deploy_branch") or ""), f"migration/{app_id.lower()}", default]) if b]
    build_dir = str(meta.get("build_dir") or meta.get("detected_build_dir") or "").strip("/")
    prefixes = [f"{build_dir}/"] if build_dir and build_dir != "." else []
    prefixes.append("")

    text, src = _repo_file(codecommit, repo, branches, [p + _SMOKE_FILE for p in prefixes])
    if text:
        try:
            raw = json.loads(text)
            items = raw.get("cases") if isinstance(raw, dict) else raw
            cases = []
            for i, c in enumerate(items or []):
                if isinstance(c, dict) and c.get("path"):
                    cases.append({**c, "name": str(c.get("name") or f"case-{i + 1}"),
                                  "method": str(c.get("method") or "GET").upper()})
            return cases, src
        except ValueError as e:
            return [{"name": "amf-smoke.json", "invalid": f"{src}: not valid JSON ({e})"}], src

    text, src = _repo_file(codecommit, repo, branches, [p + f for p in prefixes for f in _OPENAPI_FILES])
    if not text:
        return [], ""
    spec = None
    try:
        spec = json.loads(text)
    except ValueError:
        try:
            import yaml  # optional in the Lambda runtime
            spec = yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            spec = None
    if not isinstance(spec, dict):
        return [], ""
    cases = [{"name": f"GET {path}", "method": "GET", "path": path, "expect_lt": 500}
             for path, ops in (spec.get("paths") or {}).items()
             if isinstance(ops, dict) and "get" in ops and "{" not in str(path)]
    return cases, src


def _subst(value, run: str):
    if isinstance(value, str):
        return value.replace("{run}", run)
    if isinstance(value, dict):
        return {k: _subst(v, run) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, run) for v in value]
    return value


# ── transports ─────────────────────────────────────────────────────────────────

def _alb_private_ip(lb: dict, ec2) -> str:
    """A private IP of the ALB's own ENIs. An internet-facing ALB's DNS name resolves to PUBLIC IPs,
    so a probe inside the VPC would leave via the NAT gateway and arrive from the NAT's public IP,
    which the ALB security group (VPC + allow-listed CIDRs) rightly drops (APP-TE 2026-10-27). The
    private address keeps the request inside the VPC; the Host header keeps the ALB rules matching."""
    arn = str(lb.get("LoadBalancerArn") or "")
    suffix = arn.split("loadbalancer/", 1)[-1] if "loadbalancer/" in arn else ""
    if not suffix:
        return ""
    try:
        enis = ec2.describe_network_interfaces(
            Filters=[{"Name": "description", "Values": [f"ELB {suffix}"]}]).get("NetworkInterfaces", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("ALB ENI lookup failed: %s", e)
        return ""
    return next((n.get("PrivateIpAddress") for n in enis if n.get("PrivateIpAddress")), "")


def _probe_subnets_and_sg(lb: dict, ec2) -> tuple[list[str], list[str]]:
    vpc = lb.get("VpcId", "")
    subnets = []
    try:
        resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])
        for s in resp.get("Subnets", []):
            name = next((t["Value"] for t in s.get("Tags", []) if t.get("Key") == "Name"), "").lower()
            if "private-app" in name:  # vpc module: {app}-subnet-private-app-<az> (the app id may contain "app")
                subnets.append(s["SubnetId"])
    except Exception as e:  # noqa: BLE001
        logger.warning("describe_subnets failed: %s", e)
    if not subnets:
        subnets = [z["SubnetId"] for z in lb.get("AvailabilityZones", []) if z.get("SubnetId")]
    sgs = []
    try:
        resp = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                                     {"Name": "group-name", "Values": ["default"]}])
        sgs = [g["GroupId"] for g in resp.get("SecurityGroups", [])][:1]
    except Exception as e:  # noqa: BLE001
        logger.warning("describe_security_groups failed: %s", e)
    return subnets, sgs


def _probe_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("probe_handler.py", _PROBE_SOURCE.read_text())
    return buf.getvalue()


def _run_via_probe(app_id: str, lb: dict, cases: list[dict], *, prefix: str, probe_role_arn: str,
                   lam, ec2) -> list[dict]:
    """Create the in-VPC probe, run every case through it, delete it (always)."""
    if not probe_role_arn:
        raise RuntimeError("VALIDATION_PROBE_ROLE_ARN is not configured on the tool Lambda")
    subnets, sgs = _probe_subnets_and_sg(lb, ec2)
    if not subnets or not sgs:
        raise RuntimeError(f"no private subnets / default security group found in {lb.get('VpcId')}")
    slug = re.sub(r"[^a-z0-9-]+", "-", app_id.lower()).strip("-")[:30]
    name = f"{prefix}-probe-{slug}-{uuid.uuid4().hex[:6]}"
    created = False
    try:
        for attempt in range(_PROBE_CREATE_ATTEMPTS):  # a just-created role can take a few seconds to be assumable
            try:
                lam.create_function(
                    FunctionName=name, Runtime=_PROBE_RUNTIME, Handler="probe_handler.handler",
                    Role=probe_role_arn, Code={"ZipFile": _probe_zip()}, Timeout=_PROBE_TIMEOUT,
                    MemorySize=_PROBE_MEMORY,
                    VpcConfig={"SubnetIds": subnets, "SecurityGroupIds": sgs},
                    Description=f"AMF validation probe for {app_id} (deleted after use)",
                    Tags={"Application": app_id, "ManagedBy": f"{prefix}-validation"},
                )
                created = True
                break
            except Exception as e:  # noqa: BLE001
                if "cannot be assumed" in str(e) and attempt < _PROBE_CREATE_ATTEMPTS - 1:
                    time.sleep(_PROBE_CREATE_RETRY_DELAY)
                    continue
                raise
        lam.get_waiter("function_active_v2").wait(
            FunctionName=name, WaiterConfig={"Delay": _PROBE_ACTIVE_DELAY, "MaxAttempts": _PROBE_ACTIVE_ATTEMPTS})
        private_ip = _alb_private_ip(lb, ec2) if lb.get("Scheme") == "internet-facing" else ""
        host = lb["DNSName"]
        payload = {"base_url": f"http://{private_ip or host}", "timeout": _REQUEST_TIMEOUT,
                   "cases": [{**{k: c.get(k) for k in ("name", "method", "path", "body")},
                              "headers": {**(c.get("headers") or {}), **({"Host": host} if private_ip else {})}}
                             for c in cases]}
        resp = lam.invoke(FunctionName=name, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
        body = resp["Payload"].read() if hasattr(resp.get("Payload"), "read") else resp.get("Payload") or b"{}"
        if resp.get("FunctionError"):
            raise RuntimeError(f"probe function error: {body[:300]!r}")
        return (json.loads(body) or {}).get("results") or []
    finally:
        if created:
            try:
                lam.delete_function(FunctionName=name)
            except Exception as e:  # noqa: BLE001 — leave a breadcrumb; the name is unique per run
                logger.warning("probe %s not deleted: %s", name, e)


def _execute_api_url_ok(url: str, region: str) -> bool:
    """SSRF guard: https to an API Gateway default endpoint in `region` only (SigV4 creds never go elsewhere)."""
    parts = urllib.parse.urlsplit(url)
    host_re = rf"[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com"
    return (parts.scheme == "https" and not parts.username and not parts.password and parts.port is None
            and re.fullmatch(host_re, parts.hostname or "") is not None)


def _signed_request(method: str, url: str, body, headers: dict, region: str, sign: bool = True) -> dict:
    """One request from the tool Lambda (SigV4 execute-api when `sign`); same result shape as the probe."""
    if not _execute_api_url_ok(url, region):  # never file:/ftp:/custom schemes or a non-execute-api host
        return {"status": 0, "body_excerpt": "", "json_keys": [], "elapsed_ms": 0,
                "error": "URL is not an https execute-api endpoint in this region"}
    data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
    hdrs = {k: str(v) for k, v in (headers or {}).items()}
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if sign:
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        req = AWSRequest(method=method, url=url, data=data, headers=hdrs)
        SigV4Auth(boto3.Session().get_credentials(), "execute-api", region).add_auth(req)
        hdrs = dict(req.headers)
    out = {"status": 0, "body_excerpt": "", "json_keys": [], "elapsed_ms": 0, "error": ""}
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        # _execute_api_url_ok() above pins https + an execute-api host in this region.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as r:  # noqa: S310  # nosec B310 — scheme + host validated
            out["status"], raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        out["status"], raw = e.code, e.read()
    except Exception as e:  # noqa: BLE001
        out["error"], raw = f"{type(e).__name__}: {e}", b""
    out["elapsed_ms"] = int((time.monotonic() - start) * 1000)
    text = raw.decode("utf-8", "replace")
    out["body_excerpt"] = text[:300]
    try:
        parsed = json.loads(text)
        out["json_keys"] = sorted(parsed) if isinstance(parsed, dict) else []
    except ValueError:
        pass
    return out


def _api_auth(apigw, api_id: str) -> str:
    """AWS_IAM | JWT | NONE — the strictest authorization across the API's routes."""
    try:
        types = {r.get("AuthorizationType", "NONE") for r in apigw.get_routes(ApiId=api_id).get("Items", [])}
    except Exception:  # noqa: BLE001
        return "AWS_IAM"
    return "JWT" if "JWT" in types else ("AWS_IAM" if "AWS_IAM" in types else "NONE")


# ── evaluation ─────────────────────────────────────────────────────────────────

def _verdict(case: dict, res: dict) -> tuple[str, str]:
    status = int(res.get("status") or 0)
    evidence = f"{case.get('method', 'GET')} {case.get('path')} → {status or res.get('error') or 'no response'}"
    excerpt = str(res.get("body_excerpt") or "")[:_EXCERPT].replace("\n", " ")
    if excerpt:
        evidence += f" · {excerpt}"
    if not status:
        return "fail", evidence
    expect = case.get("expect_status")
    if expect is not None:
        allowed = {int(x) for x in (expect if isinstance(expect, list) else [expect])}
        if status not in allowed:
            return "fail", f"{evidence} (expected {sorted(allowed)})"
    elif status >= int(case.get("expect_lt") or 500):
        return "fail", f"{evidence} (expected < {case.get('expect_lt') or 500})"
    missing = [k for k in (case.get("expect_json_keys") or []) if k not in (res.get("json_keys") or [])]
    if missing:
        return "fail", f"{evidence} (missing JSON keys {missing})"
    return "pass", evidence


def _evidence(case: dict, res: dict, base: str, via: str, source: str) -> dict:
    """What was sent, what came back and what was expected — the Validation screen's drill-down."""
    body = case.get("body")
    body_txt = None if body is None else (body if isinstance(body, str) else json.dumps(body))
    expect = {k: case[k] for k in ("expect_status", "expect_lt", "expect_json_keys") if case.get(k) is not None}
    return {
        "request": {"method": case.get("method", "GET"), "url": base + "/" + str(case.get("path") or "/").lstrip("/"),
                    "body": (body_txt or "")[:500] or None},
        "response": {"status": int(res.get("status") or 0), "elapsed_ms": res.get("elapsed_ms"),
                     "json_keys": (res.get("json_keys") or [])[:30],
                     "body_excerpt": str(res.get("body_excerpt") or "")[:300], "error": res.get("error") or None},
        "expect": expect or {"expect_lt": 500},
        "transport": via, "source": source or None,
    }


def _ddb_av(v):
    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, (int, float)):
        return {"N": str(v)}
    return {"S": str(v)}


def _detected_table(meta: dict) -> dict:
    tables = meta.get("detected_dynamodb_tables") or []
    return tables[0] if tables and isinstance(tables[0], dict) else {}


def _app_table(meta: dict, app_id: str) -> str:
    """The table the generated IaC created for this app — the ONLY table L3 ever touches."""
    return str(meta.get("dynamodb_table_name") or _detected_table(meta).get("name") or f"{app_id}-table")


def _app_table_key(meta: dict) -> set[str]:
    """Key attribute names of the app table — mirrors tool_execution's dynamodb module wiring."""
    det = _detected_table(meta)
    hash_key = meta.get("dynamodb_hash_key") or det.get("hash_key") or ""
    range_key = (meta.get("dynamodb_range_key") or "") if meta.get("dynamodb_hash_key") else (det.get("range_key") or "")
    if not hash_key:
        hash_key, range_key = "PK", "SK"
    return {str(hash_key)} | ({str(range_key)} if range_key else set())


def _platform_table(name: str, prefix: str, control_table) -> bool:
    """True for AMF's own tables ({prefix}_portfolio, {prefix}_tf_lock, ...) — never an app's."""
    low, pre = name.lower(), (prefix or "").lower()
    own = str(getattr(control_table, "name", "") or "")
    return bool((pre and (low.startswith(f"{pre}_") or low.startswith(f"{pre}-"))) or (own and name == own))


# ── entry point ────────────────────────────────────────────────────────────────

def run_app_checks(app_id: str, meta: dict, checks: list, *, region: str, prefix: str, probe_role_arn: str,
                   s3, table, codecommit, check, client_factory=None) -> None:
    """Append L1/L2/L3 rows for a deployed app. Never raises."""
    del s3  # reserved (request/response evidence upload); rows carry excerpts today
    client = client_factory or (lambda name: _boto_client(name, region))
    try:
        _run(app_id, meta, checks, region=region, prefix=prefix, probe_role_arn=probe_role_arn,
             table=table, codecommit=codecommit, check=check, client=client)
    except Exception as e:  # noqa: BLE001 — never break Validation; report it
        logger.exception("validation probe failed for %s", app_id)
        check(checks, "L1:probe", "fail", f"application checks could not run: {type(e).__name__}: {e}")


def _find_http_api(apigw, name: str) -> dict | None:
    """The HTTP API called `name`, following NextToken — get_apis returns one page at a time."""
    kwargs: dict = {}
    while True:
        page = apigw.get_apis(**kwargs)
        api = next((a for a in page.get("Items", []) if a.get("Name") == name), None)
        if api or not page.get("NextToken"):
            return api
        kwargs = {"NextToken": page["NextToken"]}


def _run(app_id, meta, checks, *, region, prefix, probe_role_arn, table, codecommit, check, client) -> None:
    compute = str(meta.get("target_compute") or "").lower()
    run = f"{prefix}-validation-{uuid.uuid4().hex[:8]}"
    write_ok = _truthy(meta.get("validation_write_tests"))

    smoke = [dict(c, tier="L1") for c in _smoke_cases(meta)]
    contract, source = _contract_cases(app_id, meta, table, codecommit)
    if not contract:
        check(checks, "L2:contract", "skip", f"no {_SMOKE_FILE} or OpenAPI spec in the repo — add {_SMOKE_FILE} "
                                             "to assert behaviour beyond the health path")
    runnable = []
    for c in contract:
        if c.get("invalid"):
            check(checks, f"L2:{c['name']}", "fail", c["invalid"])
            continue
        c = dict(_subst(c, run), tier="L2")
        if c["method"] in _WRITE_METHODS and not write_ok:
            check(checks, f"L2:{c['name']}", "skip", "write case — set validation_write_tests=true to run it")
            continue
        runnable.append(c)
    cases = smoke + runnable

    # ── send ──
    if compute in ("ecs_fargate", "ec2"):
        elb, ec2 = client("elbv2"), client("ec2")
        try:
            lb = elb.describe_load_balancers(Names=[f"{app_id}-alb"])["LoadBalancers"][0]
        except Exception as e:  # noqa: BLE001
            check(checks, "L1:health", "fail", f"ALB {app_id}-alb not found: {e}")
            return
        results = _run_via_probe(app_id, lb, cases, prefix=prefix, probe_role_arn=probe_role_arn,
                                 lam=client("lambda"), ec2=ec2)
        via = f"in-VPC probe → {lb.get('Scheme', '')} ALB"
        base = f"http://{lb['DNSName']}"
    elif compute == "lambda":
        apigw = client("apigatewayv2")
        api = _find_http_api(apigw, f"{app_id}-api")
        if not api:
            check(checks, "L1:health", "fail", f"HTTP API {app_id}-api not found")
            return
        auth = _api_auth(apigw, api["ApiId"])
        if auth == "JWT":
            for c in cases:
                check(checks, f"{c['tier']}:{c['name']}", "skip", "JWT-protected — provide amf-smoke.json auth")
            return
        base = api["ApiEndpoint"].rstrip("/")
        results = [_signed_request(c["method"], base + "/" + str(c.get("path") or "/").lstrip("/"), c.get("body"),
                                   c.get("headers") or {}, region, sign=auth == "AWS_IAM") for c in cases]
        via = f"SigV4 → {base}" if auth == "AWS_IAM" else f"unauthenticated → {base}"
    else:
        check(checks, "L1:health", "skip", f"no application checks for target_compute={compute or '(not set)'}")
        return

    # ── evaluate ──
    ddb = None
    for c, res in zip(cases, results + [{}] * (len(cases) - len(results)), strict=False):
        result, evidence = _verdict(c, res)
        check(checks, f"{c['tier']}:{c['name']}", result, f"{evidence} [{via}{'; ' + source if c['tier'] == 'L2' else ''}]",
              evidence=_evidence(c, res, base, via, source if c["tier"] == "L2" else ""))
        verify = c.get("verify_dynamodb")
        if c["tier"] != "L2" or not verify or not write_ok:
            continue
        # amf-smoke.json is repo-controlled: its `table` is ignored and the key must be the app
        # table's own key, so a repo can't make the probe read/delete in another table.
        table_name = _app_table(meta, app_id)
        asked = verify.get("table") if isinstance(verify, dict) else None
        if asked and str(asked) != table_name:
            check(checks, f"L3:{c['name']}:table", "warn",
                  f"verify_dynamodb.table {str(asked)[:100]!r} ignored — L3 only checks the app's own table {table_name}")
        if _platform_table(table_name, prefix, table):
            check(checks, f"L3:{c['name']}", "fail", f"{table_name} is a platform table, not the app's — not checked")
            continue
        raw_key = verify.get("key") if isinstance(verify, dict) else None
        raw_key = raw_key if isinstance(raw_key, dict) else {}
        expected = _app_table_key(meta)
        if raw_key and set(map(str, raw_key)) != expected:
            check(checks, f"L3:{c['name']}", "fail",
                  f"verify_dynamodb.key {sorted(map(str, raw_key))[:10]} is not {table_name}'s key {sorted(expected)}"
                  " — not checked")
            continue
        key = {str(k): _ddb_av(v) for k, v in raw_key.items()}
        if result != "pass" or not key:
            check(checks, f"L3:{c['name']}", "fail" if key else "skip",
                  "request failed — record not checked" if key else "verify_dynamodb has no key")
            continue
        ddb = ddb or client("dynamodb")
        try:
            item = ddb.get_item(TableName=table_name, Key=key, ConsistentRead=True).get("Item")
            check(checks, f"L3:{c['name']}", "pass" if item else "fail",
                  f"{table_name} {verify.get('key')} {'present' if item else 'NOT found after the request'}",
                  evidence={"table": table_name, "key": verify.get("key"), "present": bool(item),
                            "attributes": sorted(item)[:20] if item else []})
        except Exception as e:  # noqa: BLE001
            check(checks, f"L3:{c['name']}", "fail", f"{table_name} GetItem failed: {e}")
        try:
            ddb.delete_item(TableName=table_name, Key=key)
            check(checks, "L3:cleanup", "pass", f"deleted test record {verify.get('key')} from {table_name}")
        except Exception as e:  # noqa: BLE001
            check(checks, "L3:cleanup", "warn", f"test record {verify.get('key')} left in {table_name}: {e}")
