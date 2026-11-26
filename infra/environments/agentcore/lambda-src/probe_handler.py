"""In-VPC validation probe — stdlib only.

validation_probe creates a short-lived Lambda from this file in the app's private
subnets so it can reach an internal ALB, invokes it once with the request cases,
and deletes it. It never holds AWS credentials beyond its execution role
(VPC-access only) and only talks to the given base_url.

Event:  {"base_url": "http://internal-x.elb.amazonaws.com",
         "cases": [{"name", "method", "path", "body", "headers"}],
         "timeout": 10}
Result: {"results": [{"name", "status", "body_excerpt", "json_keys", "elapsed_ms", "error"}]}
"""
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request

_EXCERPT = 300


_ALB_DNS_SUFFIX = ".elb.amazonaws.com"  # AWS contract: ALB DNS names (internal-*.elb.amazonaws.com)


def _target_error(base_url: str, url: str) -> str:
    """SSRF guard: http(s) only, the request host must be the probe's base host, and that host must be an
    ALB DNS name or a private IP (validation_probe passes the ALB's private IP + a Host header)."""
    base, target = urllib.parse.urlsplit(base_url), urllib.parse.urlsplit(url)
    if target.scheme.lower() not in ("http", "https") or base.scheme.lower() not in ("http", "https"):
        return "unsupported URL scheme"
    if target.username or target.password or (target.hostname or "") != (base.hostname or "") \
            or target.port != base.port:
        return "request host differs from the probe base_url"
    host = base.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_private or ip.is_link_local or ip.is_loopback:  # no IMDS (169.254/16) or localhost
            return "probe target IP is not a private VPC address"
    except ValueError:
        if not host.endswith(_ALB_DNS_SUFFIX):
            return "probe target is not an ALB DNS name"
    return ""


def _one(base_url: str, case: dict, timeout: float) -> dict:
    method = str(case.get("method") or "GET").upper()
    url = base_url.rstrip("/") + "/" + str(case.get("path") or "/").lstrip("/")
    body = case.get("body")
    data = None
    headers = {k: str(v) for k, v in (case.get("headers") or {}).items()}
    if body is not None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        headers.setdefault("Content-Type", "application/json")
    out = {"name": case.get("name") or f"{method} {case.get('path')}", "status": 0,
           "body_excerpt": "", "json_keys": [], "elapsed_ms": 0, "error": ""}
    err = _target_error(base_url, url)
    if err:
        out["error"] = err
        return out
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        # _target_error() above pins http(s) and the host to the app's ALB / its private IP.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310 — scheme + host validated
            out["status"], raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        out["status"], raw = e.code, e.read()
    except Exception as e:  # noqa: BLE001 — connection refused / timeout is the evidence
        out["error"], raw = f"{type(e).__name__}: {e}", b""
    out["elapsed_ms"] = int((time.monotonic() - start) * 1000)
    text = raw.decode("utf-8", "replace")
    out["body_excerpt"] = text[:_EXCERPT]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            out["json_keys"] = sorted(parsed)
    except ValueError:
        pass
    return out


def handler(event, context):  # noqa: ARG001
    base_url = event["base_url"]
    timeout = float(event.get("timeout") or 10)
    return {"results": [_one(base_url, c, timeout) for c in event.get("cases") or []]}
