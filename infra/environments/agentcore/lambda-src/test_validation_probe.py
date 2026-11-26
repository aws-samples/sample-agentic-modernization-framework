"""Validation tiers L1–L3: validation_probe.run_app_checks."""
import io
import json

import pytest
import validation_probe as vp


def _check(checks, name, result, detail, evidence=None):
    checks.append({"check": name, "result": result, "detail": detail, **({"evidence": evidence} if evidence else {})})


class FakeElb:
    def __init__(self, found=True):
        self.found = found

    def describe_load_balancers(self, Names):  # noqa: N803
        if not self.found:
            raise RuntimeError("LoadBalancerNotFound")
        return {"LoadBalancers": [{"DNSName": "internal-app.elb.amazonaws.com", "Scheme": "internal",
                                   "VpcId": "vpc-1", "AvailabilityZones": [{"SubnetId": "subnet-pub"}]}]}


class FakeEc2:
    def describe_subnets(self, Filters):  # noqa: N803
        return {"Subnets": [{"SubnetId": "subnet-app-a", "Tags": [{"Key": "Name", "Value": "APP-X-subnet-private-app-a"}]},
                            {"SubnetId": "subnet-db-a", "Tags": [{"Key": "Name", "Value": "APP-X-subnet-private-db-a"}]}]}

    def describe_security_groups(self, Filters):  # noqa: N803
        return {"SecurityGroups": [{"GroupId": "sg-default"}]}


class FakeWaiter:
    def wait(self, **_):
        return None


class FakeLambda:
    """Echoes cases with canned statuses keyed by path."""

    def __init__(self, statuses, fail_invoke=False):
        self.statuses, self.fail_invoke = statuses, fail_invoke
        self.created, self.deleted, self.payload = [], [], None

    def create_function(self, **kw):
        self.created.append(kw)

    def get_waiter(self, name):
        assert name == "function_active_v2"
        return FakeWaiter()

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        if self.fail_invoke:
            raise RuntimeError("TooManyRequests")
        self.payload = json.loads(Payload)
        results = []
        for c in self.payload["cases"]:
            status, body = self.statuses.get(c["path"], (404, "{}"))
            keys = sorted(json.loads(body)) if body.startswith("{") else []
            results.append({"name": c["name"], "status": status, "body_excerpt": body, "json_keys": keys,
                            "elapsed_ms": 3, "error": ""})
        return {"Payload": io.BytesIO(json.dumps({"results": results}).encode())}

    def delete_function(self, FunctionName):  # noqa: N803
        self.deleted.append(FunctionName)


class FakeApiGw:
    def __init__(self, auth="AWS_IAM"):
        self.auth = auth

    def get_apis(self):
        return {"Items": [{"Name": "APP-MOVIES-api", "ApiId": "a1", "ApiEndpoint": "https://a1.execute-api.us-east-1.amazonaws.com"}]}

    def get_routes(self, ApiId):  # noqa: N803
        return {"Items": [{"RouteKey": "$default", "AuthorizationType": self.auth}]}


class FakeDdb:
    def __init__(self, present=True):
        self.present, self.gets, self.deletes = present, [], []

    def get_item(self, **kw):
        self.gets.append(kw)
        return {"Item": kw["Key"]} if self.present else {}

    def delete_item(self, **kw):
        self.deletes.append(kw)


class FakeTable:
    def query(self, **_):
        return {"Items": [{"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/music-repo"}]}


class FakeCodeCommit:
    def __init__(self, files=None):
        self.files = files or {}  # (branch, path) -> text
        self.asked = []

    def get_repository(self, repositoryName):  # noqa: N803
        return {"repositoryMetadata": {"defaultBranch": "main"}}

    def get_file(self, repositoryName, filePath, commitSpecifier):  # noqa: N803
        self.asked.append((commitSpecifier, filePath))
        if (commitSpecifier, filePath) not in self.files:
            raise RuntimeError("FileDoesNotExist")
        return {"fileContent": self.files[(commitSpecifier, filePath)].encode()}


SMOKE = json.dumps([
    {"name": "root", "method": "GET", "path": "/", "expect_status": 200},
    {"name": "create", "method": "POST", "path": "/v1/bestmusic/90s",
     "body": {"artist": "{run}", "song": "Song 2"}, "expect_status": 200, "expect_json_keys": ["artist", "song"],
     "verify_dynamodb": {"key": {"artist": "{run}"}}},
    {"name": "missing", "method": "GET", "path": "/v1/bestmusic/90s/nobody", "expect_status": 404},
])


def _run(meta, clients, codecommit=None, probe_role="arn:aws:iam::1:role/p"):
    checks = []
    vp.run_app_checks(meta["app_id"], meta, checks, region="us-east-1", prefix="uat", probe_role_arn=probe_role,
                      s3=None, table=FakeTable(), codecommit=codecommit or FakeCodeCommit(), check=_check,
                      client_factory=lambda name: clients[name])
    return {c["check"]: c for c in checks}, checks


def _ecs_meta(**kw):
    return {"app_id": "APP-MUSIC", "target_compute": "ecs_fargate", "health_check_path": "/",
            "dynamodb_table_name": "musicTable", "dynamodb_hash_key": "artist", **kw}


def test_internal_alb_runs_through_the_probe_and_always_deletes_it():
    lam = FakeLambda({"/": (200, "Hello World!")})
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam})
    assert rows["L1:health"]["result"] == "pass" and "in-VPC probe" in rows["L1:health"]["detail"]
    fn = lam.created[0]
    assert fn["FunctionName"].startswith("uat-probe-app-music-")
    assert fn["VpcConfig"] == {"SubnetIds": ["subnet-app-a"], "SecurityGroupIds": ["sg-default"]}
    assert lam.payload["base_url"] == "http://internal-app.elb.amazonaws.com"
    assert lam.deleted == [fn["FunctionName"]]
    assert rows["L2:contract"]["result"] == "skip"  # no amf-smoke.json


def test_probe_is_deleted_even_when_the_invoke_fails():
    lam = FakeLambda({}, fail_invoke=True)
    rows, checks = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam})
    assert lam.deleted and lam.deleted[0] == lam.created[0]["FunctionName"]
    assert rows["L1:probe"]["result"] == "fail" and "TooManyRequests" in rows["L1:probe"]["detail"]


def test_unhealthy_app_fails_l1_with_evidence():
    lam = FakeLambda({"/": (502, "Bad Gateway")})
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam})
    assert rows["L1:health"]["result"] == "fail"
    assert "→ 502" in rows["L1:health"]["detail"] and "Bad Gateway" in rows["L1:health"]["detail"]


def test_missing_alb_is_a_failed_row_not_an_exception():
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(found=False), "ec2": FakeEc2(), "lambda": FakeLambda({})})
    assert rows["L1:health"]["result"] == "fail" and "APP-MUSIC-alb" in rows["L1:health"]["detail"]


def test_amf_smoke_write_cases_skip_unless_opted_in():
    cc = FakeCodeCommit({("migration/app-music", "amf-smoke.json"): SMOKE})
    lam = FakeLambda({"/": (200, "ok"), "/v1/bestmusic/90s/nobody": (404, '{"error": "x"}')})
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam}, codecommit=cc)
    assert rows["L2:root"]["result"] == "pass" and rows["L2:missing"]["result"] == "pass"
    assert rows["L2:create"]["result"] == "skip" and "validation_write_tests" in rows["L2:create"]["detail"]
    assert all(c["method"] != "POST" for c in lam.payload["cases"])
    assert not any(k.startswith("L3:") for k in rows)


def test_l3_round_trip_checks_the_table_and_cleans_up():
    cc = FakeCodeCommit({("migration/app-music", "amf-smoke.json"): SMOKE})
    lam = FakeLambda({"/": (200, "ok"), "/v1/bestmusic/90s": (200, '{"artist": "a", "song": "s"}'),
                      "/v1/bestmusic/90s/nobody": (404, "{}")})
    ddb = FakeDdb(present=True)
    rows, _ = _run(_ecs_meta(validation_write_tests=True),
                   {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam, "dynamodb": ddb}, codecommit=cc)
    post = next(c for c in lam.payload["cases"] if c["method"] == "POST")
    run_id = post["body"]["artist"]
    assert run_id.startswith("uat-validation-")  # {run} substituted, named from the project prefix
    assert rows["L2:create"]["result"] == "pass"
    assert rows["L3:create"]["result"] == "pass"
    assert ddb.gets[0] == {"TableName": "musicTable", "Key": {"artist": {"S": run_id}}, "ConsistentRead": True}
    assert ddb.deletes == [{"TableName": "musicTable", "Key": {"artist": {"S": run_id}}}]
    assert rows["L3:cleanup"]["result"] == "pass"


def test_l3_fails_when_the_record_never_landed():
    cc = FakeCodeCommit({("migration/app-music", "amf-smoke.json"): SMOKE})
    lam = FakeLambda({"/": (200, "ok"), "/v1/bestmusic/90s": (200, '{"artist": "a", "song": "s"}')})
    rows, _ = _run(_ecs_meta(validation_write_tests=True),
                   {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam, "dynamodb": FakeDdb(present=False)},
                   codecommit=cc)
    assert rows["L3:create"]["result"] == "fail" and "NOT found" in rows["L3:create"]["detail"]


def test_expected_json_keys_are_enforced():
    smoke = json.dumps([{"name": "keys", "path": "/", "expect_status": 200, "expect_json_keys": ["status"]}])
    cc = FakeCodeCommit({("migration/app-music", "amf-smoke.json"): smoke})
    lam = FakeLambda({"/": (200, '{"ok": true}')})
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam}, codecommit=cc)
    assert rows["L2:keys"]["result"] == "fail" and "missing JSON keys ['status']" in rows["L2:keys"]["detail"]


def test_smoke_file_in_build_dir_wins_and_deploy_branch_is_used():
    cc = FakeCodeCommit({("release", "svc/amf-smoke.json"): json.dumps([{"name": "a", "path": "/"}]),
                         ("release", "amf-smoke.json"): json.dumps([{"name": "b", "path": "/"}])})
    lam = FakeLambda({"/": (200, "ok")})
    rows, _ = _run(_ecs_meta(deploy_branch="release", build_dir="svc"),
                   {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam}, codecommit=cc)
    assert "L2:a" in rows and "L2:b" not in rows


def test_openapi_get_operations_become_l2_cases():
    spec = json.dumps({"openapi": "3.0.0", "paths": {"/items": {"get": {}}, "/items/{id}": {"get": {}},
                                                     "/orders": {"post": {}}}})
    cc = FakeCodeCommit({("migration/app-music", "openapi.json"): spec})
    lam = FakeLambda({"/": (200, "ok"), "/items": (200, "[]")})
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam}, codecommit=cc)
    assert rows["L2:GET /items"]["result"] == "pass"
    assert not any("{id}" in k or "/orders" in k for k in rows)


def test_invalid_smoke_file_is_a_failed_row():
    cc = FakeCodeCommit({("migration/app-music", "amf-smoke.json"): "{not json"})
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": FakeLambda({"/": (200, "")})},
                   codecommit=cc)
    assert rows["L2:amf-smoke.json"]["result"] == "fail"


def test_lambda_target_uses_sigv4_requests(monkeypatch):
    calls = []

    def fake_request(method, url, body, headers, region, sign=True):
        calls.append((method, url, sign))
        return {"status": 200, "body_excerpt": '{"message": "ok"}', "json_keys": ["message"], "elapsed_ms": 5, "error": ""}

    monkeypatch.setattr(vp, "_signed_request", fake_request)
    meta = {"app_id": "APP-MOVIES", "target_compute": "lambda", "detected_health_check_path": "/",
            "detected_lambda_functions": [{"name": "fn", "routes": ["GET /movies", "GET /movies/{id}", "POST /movies"]}]}
    rows, _ = _run(meta, {"apigatewayv2": FakeApiGw("AWS_IAM")})
    assert rows["L1:health"]["result"] == "pass" and "SigV4" in rows["L1:health"]["detail"]
    assert rows["L1:GET /movies"]["result"] == "pass"
    assert ("GET", "https://a1.execute-api.us-east-1.amazonaws.com/", True) in calls
    assert not any("{id}" in u for _, u, _ in calls)


def test_jwt_protected_api_is_skipped():
    meta = {"app_id": "APP-MOVIES", "target_compute": "lambda"}
    rows, _ = _run(meta, {"apigatewayv2": FakeApiGw("JWT")})
    assert rows["L1:health"]["result"] == "skip" and "JWT" in rows["L1:health"]["detail"]


def test_missing_probe_role_is_reported():
    rows, _ = _run(_ecs_meta(), {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": FakeLambda({})}, probe_role="")
    assert rows["L1:probe"]["result"] == "fail" and "VALIDATION_PROBE_ROLE_ARN" in rows["L1:probe"]["detail"]


@pytest.mark.parametrize("compute", ["", "eks"])
def test_unknown_compute_is_skipped(compute):
    rows, _ = _run({"app_id": "APP-X", "target_compute": compute}, {})
    assert rows["L1:health"]["result"] == "skip"


def test_probe_handler_reports_status_and_json_keys(monkeypatch):
    import probe_handler

    class Resp:
        status = 200

        def __init__(self):
            self.body = b'{"artist": "a", "song": "s"}'

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def fake_urlopen(req, timeout):
        seen.append((req.get_method(), req.full_url, req.data))
        return Resp()

    monkeypatch.setattr(probe_handler.urllib.request, "urlopen", fake_urlopen)
    out = probe_handler.handler({"base_url": "http://internal-a.us-east-1.elb.amazonaws.com/", "cases": [
        {"name": "c", "method": "POST", "path": "/v1/x", "body": {"a": 1}}]}, None)
    assert out["results"][0]["status"] == 200 and out["results"][0]["json_keys"] == ["artist", "song"]
    assert seen == [("POST", "http://internal-a.us-east-1.elb.amazonaws.com/v1/x", b'{"a": 1}')]


def test_probe_handler_refuses_off_target_urls(monkeypatch):
    """SSRF guard: only the ALB DNS name / its private IP, http(s), and the path can't change the host."""
    import probe_handler

    fetched = []
    monkeypatch.setattr(probe_handler.urllib.request, "urlopen", lambda *a, **k: fetched.append(a))
    for base, path in (("http://169.254.169.254", "/latest/meta-data"),     # link-local, not private-ALB
                       ("http://example.com", "/"),                         # not an ALB
                       ("file:///etc", "passwd"),
                       ("http://8.8.8.8", "/")):                            # public IP
        res = probe_handler.handler({"base_url": base, "cases": [{"name": "c", "path": path}]}, None)["results"][0]
        assert res["status"] == 0 and res["error"] and not fetched, (base, path)
    assert probe_handler._target_error("http://10.0.1.5", "http://10.0.1.5/health") == ""
    assert probe_handler._target_error("http://10.0.1.5", "http://evil.com/x") != ""


def test_signed_request_only_targets_execute_api_in_region(monkeypatch):
    fetched = []
    monkeypatch.setattr(vp.urllib.request, "urlopen", lambda *a, **k: fetched.append(a))
    for url in ("http://abc123.execute-api.us-east-1.amazonaws.com/x",      # not https
                "https://abc123.execute-api.us-west-2.amazonaws.com/x",     # other region
                "https://evil.com/x", "https://u:p@abc123.execute-api.us-east-1.amazonaws.com/x",
                "file:///etc/passwd"):
        res = vp._signed_request("GET", url, None, {}, "us-east-1", sign=False)
        assert res["status"] == 0 and "execute-api" in res["error"] and not fetched, url
    assert vp._execute_api_url_ok("https://abc123.execute-api.us-east-1.amazonaws.com/v1/x", "us-east-1")


def test_multi_function_api_health_is_its_first_parameter_free_get(monkeypatch):
    """APP-CLI (products API): no /health route — /health would 404 and fail Validation."""
    calls = []
    monkeypatch.setattr(vp, "_signed_request", lambda m, u, b, h, r, sign=True: calls.append(u) or
                        {"status": 200, "body_excerpt": "[]", "json_keys": [], "elapsed_ms": 5, "error": ""})
    meta = {"app_id": "APP-MOVIES", "target_compute": "lambda",
            "detected_lambda_functions": [{"name": "a", "routes": ["GET /products/{id}"]},
                                          {"name": "b", "routes": ["GET /products"]}]}
    rows, _ = _run(meta, {"apigatewayv2": FakeApiGw("AWS_IAM")})
    assert rows["L1:health"]["result"] == "pass", rows["L1:health"]
    assert "GET /products" in rows["L1:health"]["detail"]
    assert not any(u.endswith("/health") for u in calls)


def test_contract_on_the_default_branch_is_found():
    """APP-CLI: the migration branch was cut from a feature branch older than the contract on main."""
    cc = FakeCodeCommit({("main", "amf-smoke.json"): SMOKE})
    cases, src = vp._contract_cases("APP-MUSIC", {}, FakeTable(), cc)
    assert src == "amf-smoke.json@main" and len(cases) == 3
    assert cc.asked[0][0] == "migration/app-music"  # the migration branch is still tried first


def test_app_checks_carry_request_response_evidence(monkeypatch):
    monkeypatch.setattr(vp, "_signed_request", lambda m, u, b, h, r, sign=True: {
        "status": 200, "body_excerpt": '{"message": "ok"}', "json_keys": ["message"], "elapsed_ms": 42, "error": ""})
    meta = {"app_id": "APP-MOVIES", "target_compute": "lambda", "detected_health_check_path": "/"}
    rows, _ = _run(meta, {"apigatewayv2": FakeApiGw("AWS_IAM")})
    ev = rows["L1:health"]["evidence"]
    assert ev["request"] == {"method": "GET", "url": "https://a1.execute-api.us-east-1.amazonaws.com/", "body": None}
    assert ev["response"]["status"] == 200 and ev["response"]["elapsed_ms"] == 42 and ev["response"]["json_keys"] == ["message"]
    assert ev["expect"] == {"expect_status": 200} and ev["transport"].startswith("SigV4")


class PublicElb(FakeElb):
    def describe_load_balancers(self, Names):  # noqa: N803
        return {"LoadBalancers": [{"DNSName": "APP-TE-alb-1.us-east-1.elb.amazonaws.com", "Scheme": "internet-facing",
                                   "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/APP-TE-alb/abc",
                                   "VpcId": "vpc-1", "AvailabilityZones": [{"SubnetId": "subnet-pub"}]}]}


class EniEc2(FakeEc2):
    def describe_network_interfaces(self, Filters):  # noqa: N803
        assert Filters == [{"Name": "description", "Values": ["ELB app/APP-TE-alb/abc"]}]
        return {"NetworkInterfaces": [{"PrivateIpAddress": "10.31.0.42"}]}


def test_internet_facing_alb_is_probed_on_its_private_ip_with_the_host_header():
    """APP-TE 2026-10-27: via the public DNS the probe left through the NAT and the ALB SG dropped it."""
    lam = FakeLambda({"/manage/health": (200, "{\"status\": \"UP\"}")})
    rows, _ = _run(_ecs_meta(health_check_path="/manage/health"), {"elbv2": PublicElb(), "ec2": EniEc2(), "lambda": lam})
    assert lam.payload["base_url"] == "http://10.31.0.42"
    assert all(c["headers"]["Host"] == "APP-TE-alb-1.us-east-1.elb.amazonaws.com" for c in lam.payload["cases"])
    assert rows["L1:health"]["result"] == "pass"


# ── a repo-controlled amf-smoke.json can't steer L3 at another table ──

class _NamedTable(FakeTable):
    name = "uat_portfolio"


def _l3_smoke(verify):
    return json.dumps([{"name": "create", "method": "POST", "path": "/v1/bestmusic/90s",
                        "body": {"artist": "{run}"}, "expect_status": 200, "verify_dynamodb": verify}])


def _l3_run(verify, **meta_kw):
    cc = FakeCodeCommit({("migration/app-music", "amf-smoke.json"): _l3_smoke(verify)})
    lam = FakeLambda({"/": (200, "ok"), "/v1/bestmusic/90s": (200, '{"artist": "a"}')})
    ddb = FakeDdb(present=True)
    clients = {"elbv2": FakeElb(), "ec2": FakeEc2(), "lambda": lam, "dynamodb": ddb}
    checks = []
    vp.run_app_checks("APP-MUSIC", _ecs_meta(validation_write_tests=True, **meta_kw), checks, region="us-east-1",
                      prefix="uat", probe_role_arn="arn:aws:iam::1:role/p", s3=None, table=_NamedTable(),
                      codecommit=cc, check=_check, client_factory=lambda n: clients[n])
    return {c["check"]: c for c in checks}, ddb


@pytest.mark.parametrize("other", ["uat_portfolio", "uat_tf_lock", "arn:aws:dynamodb:us-east-1:1:table/uat_portfolio"])
def test_verify_table_is_ignored_only_the_app_table_is_touched(other):
    rows, ddb = _l3_run({"table": other, "key": {"artist": "{run}"}})
    touched = {c["TableName"] for c in ddb.gets + ddb.deletes}
    assert touched == {"musicTable"}
    assert rows["L3:create:table"]["result"] == "warn" and "ignored" in rows["L3:create:table"]["detail"]
    assert rows["L3:create"]["result"] == "pass"


@pytest.mark.parametrize("key", [{"PK": "APP#APP-X", "SK": "META"}, {"artist": "{run}", "PK": "APP#APP-X"},
                                 {"LockID": "uat-tfstate/terraform.tfstate"}])
def test_key_must_be_the_app_tables_key(key):
    rows, ddb = _l3_run({"table": "uat_portfolio", "key": key})
    assert not ddb.gets and not ddb.deletes
    assert rows["L3:create"]["result"] == "fail" and "not checked" in rows["L3:create"]["detail"]


@pytest.mark.parametrize("name", ["uat_portfolio", "uat-anything", "UAT_tf_lock"])
def test_app_table_resolving_to_a_platform_table_is_refused(name):
    # e.g. a repo whose code "detects" the control-plane table as its own
    rows, ddb = _l3_run({"key": {"PK": "APP#APP-X", "SK": "META"}}, dynamodb_table_name=name,
                        dynamodb_hash_key="PK", dynamodb_range_key="SK")
    assert not ddb.gets and not ddb.deletes
    assert rows["L3:create"]["result"] == "fail" and "platform table" in rows["L3:create"]["detail"]


def test_default_app_table_key_is_pk_sk():
    assert vp._app_table_key({}) == {"PK", "SK"}
    assert vp._app_table_key({"detected_dynamodb_tables": [{"name": "t", "hash_key": "id"}]}) == {"id"}
    assert vp._app_table({"detected_dynamodb_tables": [{"name": "movies"}]}, "APP-M") == "movies"
