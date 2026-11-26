#!/usr/bin/env python3
"""Regenerate THIRD-PARTY-LICENSES from the installed dependencies.

Production dependencies only:
  - Python: the transitive closure of pyproject.toml [project].dependencies, read from the
    backend virtualenv's installed package metadata (run after `pip install -e .` and `pnpm install`).
  - Frontend: `pnpm licenses list --prod --json` in frontend/.
Build-time downloads in docker/Dockerfile.worker are listed in the fixed header below.

Usage: python3 scripts/third_party_licenses.py [--check]
  --check  exit 1 if the committed file is out of date (no write).
"""
from __future__ import annotations

import importlib.metadata as md
import json
import re
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "THIRD-PARTY-LICENSES"

HEADER = """\
THIRD-PARTY LICENSES
====================

AMF is licensed under MIT-0 (see LICENSE). It depends on the third-party software listed
below, each under its own license. Regenerate this file with
`python3 scripts/third_party_licenses.py`.

Licenses that need attention
----------------------------
- certifi (Python, MPL-2.0): used unmodified; source at https://github.com/certifi/python-certifi
- orjson (Python, MPL-2.0 AND (Apache-2.0 OR MIT)): used unmodified, if installed
- dompurify (JavaScript, MPL-2.0 OR Apache-2.0): used under Apache-2.0
- caniuse-lite (JavaScript, CC-BY-4.0): browser-support data used at build time;
  attribution: "caniuse-lite" by Ben Briggs and contributors, data from caniuse.com
- khroma (JavaScript, MIT): a mermaid dependency (its license file is named "license")

Downloaded when the worker image is built (docker/Dockerfile.worker; not stored in this repository)
--------------------------------------------------------------------------------------------------
- Terraform CLI (HashiCorp): Business Source License 1.1 (BUSL-1.1)
- AWS CLI v2: Apache-2.0
- AWS Transform CLI (atx): AWS Service Terms / AWS Customer Agreement
- Amazon Corretto 21 JDK: GPL-2.0 with the Classpath Exception
- Apache Maven: Apache-2.0
- Node.js: MIT
- Base image python:3.12-slim (Debian): Python Software Foundation License and the Debian package licenses

"""

_PERMISSIVE = re.compile(r"MIT|BSD|Apache|ISC|PSF|Python Software|Unlicense|0BSD|zlib|MPL|CC-BY", re.I)


def _license(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    if classifiers:
        return " / ".join(dict.fromkeys(classifiers))
    lic = (meta.get("License") or "").strip().splitlines()
    return lic[0][:80] if lic else "UNKNOWN"


def _name(req: str) -> str:
    return re.split(r"[\s\[<>=!~;(]", req.strip(), maxsplit=1)[0].lower().replace("_", "-")


def python_runtime() -> list[tuple[str, str, str]]:
    roots = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    seen: dict[str, md.Distribution] = {}
    todo = [_name(r) for r in roots]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        seen[name] = dist
        for req in dist.requires or []:
            if "extra ==" in req:  # optional extras are not installed by default
                continue
            todo.append(_name(req))
    return sorted((d.metadata["Name"], d.version, _license(d)) for d in seen.values())


def frontend_prod() -> list[tuple[str, str, str]]:
    out = subprocess.run(["pnpm", "licenses", "list", "--prod", "--json"], cwd=ROOT / "frontend",  # nosec B603 B607 — list argv, no shell; arguments are fixed or validated; tool resolved from PATH (git/pnpm) inside a controlled image or the developer's checkout
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for lic, pkgs in json.loads(out).items():
        for p in pkgs:
            lic_name = "MIT" if (p["name"] == "khroma" and lic == "Unknown") else lic
            for version in p.get("versions") or [p.get("version", "")]:
                rows.append((p["name"], version, lic_name))
    return sorted(set(rows))


def render() -> str:
    lines = [HEADER]
    for title, rows in (("Python runtime dependencies", python_runtime()),
                        ("Frontend production dependencies", frontend_prod())):
        lines += [title, "-" * len(title)]
        lines += [f"{n} {v}: {lic}" for n, v, lic in rows]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    text = render()
    unknown = [ln for ln in text.splitlines() if ln.endswith(": UNKNOWN")]
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print("THIRD-PARTY-LICENSES is out of date; run scripts/third_party_licenses.py")
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}; entries without license metadata: {len(unknown)}")
    for ln in unknown:
        print("  ", ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
