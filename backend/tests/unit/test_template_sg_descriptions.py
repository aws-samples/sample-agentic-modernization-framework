"""EC2 rejects security group / rule descriptions outside a narrow character set (CreateSecurityGroup
InvalidParameterValue). APP-HE's first deploy failed on "the app's CIDRs" (an apostrophe) in the RDS
module, after the VPC, NAT and ALB were already created. Every generated template must pass."""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[3] / "backend" / "resources" / "templates" / "terraform"
# EC2 API: "a-z A-Z 0-9 spaces ._-:/()#,@[]+=&;{}!$*", fewer than 256 characters.
ALLOWED = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]{0,255}$")
SG_BLOCK = re.compile(r'resource "aws_(?:security_group|security_group_rule|vpc_security_group_(?:ingress|egress)_rule)"')


def _sg_descriptions():
    for f in sorted(TEMPLATES.rglob("*.tf")) + sorted(TEMPLATES.rglob("*.tpl")):
        text = f.read_text()
        for m in SG_BLOCK.finditer(text):
            depth, i = 0, text.index("{", m.end())
            start = i
            while True:  # the resource block, braces balanced (descriptions never contain braces here)
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
                if depth == 0:
                    break
            for d in re.findall(r'^\s*description\s*=\s*"(.*)"\s*$', text[start:i], re.M):
                yield f.relative_to(TEMPLATES), d


def test_templates_have_security_groups():
    assert list(_sg_descriptions())


def test_security_group_descriptions_use_only_ec2_allowed_characters():
    bad = [(str(f), d) for f, d in _sg_descriptions() if not ALLOWED.match(re.sub(r"\$\{[^}]*\}", "x", d))]
    assert not bad, bad
