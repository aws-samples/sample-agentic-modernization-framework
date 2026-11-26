"""Network-exposure policy for generated infrastructure (pure, stdlib only).

Evaluates a `terraform show -json <planfile>` document and returns the resources
that would make a migrated service reachable from the internet. Used by the
infra worker (InfrastructureValidate) so every application's generated IaC is
held to the same rule — "no service open / exposed" — before Execution can
reach its approval gate. A public endpoint is only acceptable as an explicit,
reviewed per-app opt-in (META expose_publicly), which turns findings into
warnings instead of failures.
"""

from __future__ import annotations

_OPEN = {"0.0.0.0/0", "::/0"}


def _after(rc: dict) -> dict:
    change = rc.get("change") or {}
    if "delete" in (change.get("actions") or []) and not change.get("after"):
        return {}
    return change.get("after") or {}


def _open_cidrs(*lists) -> list[str]:
    return sorted({c for lst in lists for c in (lst or []) if c in _OPEN})


def evaluate(plan: dict) -> list[dict]:
    """[{address, type, rule, detail}] for every exposure in the planned state."""
    findings: list[dict] = []

    def add(rc, rule, detail):
        findings.append({"address": rc.get("address", ""), "type": rc.get("type", ""), "rule": rule, "detail": detail})

    for rc in plan.get("resource_changes") or []:
        a = _after(rc)
        if not a:
            continue
        t = rc.get("type", "")
        if t in ("aws_lb", "aws_alb", "aws_elb") and a.get("internal") is False:
            add(rc, "public-load-balancer", "load balancer is internet-facing (internal = false)")
        elif t == "aws_security_group":
            for ing in a.get("ingress") or []:
                cidrs = _open_cidrs(ing.get("cidr_blocks"), ing.get("ipv6_cidr_blocks"))
                if cidrs:
                    add(rc, "open-ingress", f"ingress {ing.get('from_port')}-{ing.get('to_port')}/{ing.get('protocol')} "
                                            f"from {', '.join(cidrs)}")
        elif t == "aws_security_group_rule" and a.get("type") == "ingress":
            cidrs = _open_cidrs(a.get("cidr_blocks"), a.get("ipv6_cidr_blocks"))
            if cidrs:
                add(rc, "open-ingress", f"ingress {a.get('from_port')}-{a.get('to_port')} from {', '.join(cidrs)}")
        elif t == "aws_vpc_security_group_ingress_rule":
            cidrs = _open_cidrs([a.get("cidr_ipv4")], [a.get("cidr_ipv6")])
            if cidrs:
                add(rc, "open-ingress", f"ingress {a.get('from_port')}-{a.get('to_port')} from {', '.join(cidrs)}")
        elif t == "aws_ecs_service":
            if any(n.get("assign_public_ip") for n in a.get("network_configuration") or []):
                add(rc, "public-ip", "ECS tasks get a public IP (assign_public_ip = true)")
        elif t == "aws_instance" and a.get("associate_public_ip_address") is True:
            add(rc, "public-ip", "EC2 instance gets a public IP")
        elif t in ("aws_launch_template",):
            if any(str(n.get("associate_public_ip_address")).lower() == "true" for n in a.get("network_interfaces") or []):
                add(rc, "public-ip", "launch template assigns a public IP")
        elif t in ("aws_db_instance", "aws_rds_cluster_instance") and a.get("publicly_accessible") is True:
            add(rc, "public-database", "database is publicly accessible")
        elif t == "aws_s3_bucket_public_access_block":
            off = [k for k in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets")
                   if a.get(k) is False]
            if off:
                add(rc, "public-bucket", "public access block disabled: " + ", ".join(off))
        elif t == "aws_s3_bucket_acl" and str(a.get("acl") or "").startswith("public"):
            add(rc, "public-bucket", f"bucket ACL {a.get('acl')}")
        elif t == "aws_lambda_function_url" and a.get("authorization_type") == "NONE":
            add(rc, "unauthenticated-endpoint", "Lambda function URL with authorization_type NONE")
        elif t == "aws_apigatewayv2_route" and (a.get("authorization_type") or "NONE") == "NONE":
            add(rc, "unauthenticated-endpoint", f"API route {a.get('route_key')} has no authorizer")
    return findings
