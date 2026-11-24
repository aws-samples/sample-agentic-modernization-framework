"""Detect an app's runtime wiring from its source.

Execution needs the port, health path, Lambda handler, DynamoDB table/key and
env vars an app actually uses. For APP-MUSIC / APP-MOVIES (2026-10-21) every one
was hand-entered and the defaults (8080, /health, app.handler, PK/SK) were wrong.
`scan_wiring` reads them from the cloned repo — file system only, no network —
and records where each value came from (`sources`).

Language detectors are per language; only Python is implemented (Java is the
next iteration: add a `_scan_java` and dispatch on `language`).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from backend.src.config import settings

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "target", "build", "dist",
              ".tox", "cdk.out", ".pytest_cache", ".aws-sam", ".mypy_cache", ".ruff_cache", "site-packages"}
_MAX_FILES = settings.repo_scan_max_files
_MAX_BYTES = settings.repo_scan_max_bytes
_HEALTH_PREFERENCE = ("/health", "/healthz", "/healthcheck", "/ping", "/status", "/")
_SAM_FUNCTION = "AWS::Serverless::Function"


# ── file helpers ────────────────────────────────────────────────────────────

def _norm(build_dir) -> str:
    v = str(build_dir or "").strip().strip("/")
    return "" if v in ("", ".") else v


def _files(root: Path, suffixes: tuple[str, ...] | None = None, names: tuple[str, ...] | None = None) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    stack = [root]
    while stack and len(out) < _MAX_FILES:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in _SKIP_DIRS and not p.name.startswith("."):
                    stack.append(p)
            elif (suffixes and p.suffix in suffixes) or (names and p.name in names):
                out.append(p)
    return out


def _read(p: Path) -> str:
    try:
        if p.stat().st_size > _MAX_BYTES:
            return ""
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _rel(repo: Path, p: Path) -> str:
    try:
        return p.relative_to(repo).as_posix()
    except ValueError:
        return p.as_posix()


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name in ("test.py", "tests.py", "conftest.py"))


# ── YAML (SAM / CloudFormation) ─────────────────────────────────────────────

def _load_cfn_yaml(text: str):
    """yaml.safe_load that tolerates CloudFormation short-form tags (!Ref, !Sub, …)."""
    try:
        import yaml
    except ImportError:  # pragma: no cover — PyYAML ships with uvicorn[standard]
        return None

    class _CfnLoader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {f"!{suffix}": value}

    _CfnLoader.add_multi_constructor("!", _tag)
    try:
        return yaml.load(text, Loader=_CfnLoader)  # noqa: S506  # nosec B506 — _CfnLoader subclasses yaml.SafeLoader
    except Exception:  # noqa: BLE001 — not a valid template
        return None


def _cfn_str(v) -> str:
    """Render a CFN value: plain strings as is, intrinsics as ${Fn:arg}."""
    if isinstance(v, dict) and len(v) == 1:
        (k, arg), = v.items()
        return "${%s:%s}" % (k.lstrip("!").replace("Fn::", ""), arg if isinstance(arg, str) else "…")
    return "" if v is None else str(v)


_SAM_CANONICAL = ("template.yaml", "template.yml", "template.json")


def _sam_templates(scan_root: Path) -> list[Path]:
    """SAM/serverless templates; in a folder with the canonical template.yaml, its
    variants (template.native.yaml, template.snapstart.yaml, …) are alternative
    builds of the SAME functions and are skipped — merging them duplicated every
    function with the native runtimes (serverless-java-frameworks-samples)."""
    out = []
    for p in _files(scan_root, (".yaml", ".yml", ".json")):
        if p.name.lower().startswith("template") or p.name in ("serverless.yml", "serverless.yaml"):
            out.append(p)
    canonical_dirs = {p.parent for p in out if p.name.lower() in _SAM_CANONICAL}
    return [p for p in out if p.parent not in canonical_dirs or p.name.lower() in _SAM_CANONICAL
            or p.name.startswith("serverless.")]


def _parse_sam(path: Path) -> dict:
    """{functions:[...], tables:[...], code_uris:[...]} from a SAM/CFN template."""
    doc = _load_cfn_yaml(_read(path))
    if not isinstance(doc, dict) or not isinstance(doc.get("Resources"), dict):
        return {}
    glob = ((doc.get("Globals") or {}).get("Function") or {}) if isinstance(doc.get("Globals"), dict) else {}
    genv = ((glob.get("Environment") or {}).get("Variables") or {}) if isinstance(glob, dict) else {}
    functions, tables, code_uris = [], [], []
    for name, res in doc["Resources"].items():
        if not isinstance(res, dict):
            continue
        rtype, props = res.get("Type"), res.get("Properties") or {}
        if rtype == _SAM_FUNCTION:
            env = {**{k: _cfn_str(v) for k, v in genv.items()},
                   **{k: _cfn_str(v) for k, v in ((props.get("Environment") or {}).get("Variables") or {}).items()}}
            routes, event_types = [], set()
            for ev in (props.get("Events") or {}).values():
                if isinstance(ev, dict) and ev.get("Type") in ("Api", "HttpApi"):
                    ep = ev.get("Properties") or {}
                    method = str(ep.get("Method") or "ANY").upper()
                    routes.append(f"{'ANY' if method == 'ANY' else method} {ep.get('Path') or '/'}")
                    event_types.add(ev["Type"])
            fn = {"name": name, "handler": props.get("Handler") or glob.get("Handler") or "",
                  "routes": routes, "env": env,
                  "memory": props.get("MemorySize") or glob.get("MemorySize"),
                  "timeout": props.get("Timeout") or glob.get("Timeout"),
                  "runtime": props.get("Runtime") or glob.get("Runtime") or ""}
            fn = {k: v for k, v in fn.items() if v not in (None, "", [], {})}
            # HTTP API events deliver payload v2.0; REST (Api) or unknown → v1.0.
            fn["payload_format"] = "2.0" if event_types == {"HttpApi"} else "1.0"
            functions.append(fn)
            uri = props.get("CodeUri") or glob.get("CodeUri")
            if isinstance(uri, str):
                code_uris.append(uri)
        elif rtype == "AWS::Serverless::SimpleTable":
            pk = (props.get("PrimaryKey") or {}).get("Name") or "id"
            tables.append({"logical_id": name, "name": _cfn_str(props.get("TableName")) or "", "hash_key": pk})
        elif rtype == "AWS::DynamoDB::Table":
            ks = {k.get("KeyType"): k.get("AttributeName") for k in props.get("KeySchema") or [] if isinstance(k, dict)}
            tables.append({"logical_id": name, "name": _cfn_str(props.get("TableName")) or "",
                           "hash_key": ks.get("HASH") or "", "range_key": ks.get("RANGE") or ""})
    # `PRODUCT_TABLE_NAME: !Ref ProductsTable` names the env var the code reads the table from.
    for t in tables:
        ref = "${Ref:%s}" % t["logical_id"]
        t["env_var"] = next((k for f in functions for k, v in f.get("env", {}).items() if v == ref), "")
    return {"functions": functions, "tables": tables, "code_uris": code_uris}


def _serverless_handlers(path: Path) -> list[str]:
    doc = _load_cfn_yaml(_read(path))
    fns = (doc or {}).get("functions") if isinstance(doc, dict) else None
    return [f.get("handler") for f in (fns or {}).values() if isinstance(f, dict) and f.get("handler")]


# ── CDK (Python) ────────────────────────────────────────────────────────────

_CDK_ASSET = re.compile(r"Code\.from_asset\(\s*(?:path\s*=\s*)?['\"]([^'\"]+)['\"]")
_CDK_HANDLER = re.compile(r"\bhandler\s*=\s*['\"]([\w./-]+\.[\w]+)['\"]")
_CDK_TABLE = re.compile(r"\bdynamodb\w*\.(?:Table|TableV2)\(", re.S)
_CDK_PK = re.compile(r"partition_key\s*=\s*\w+\.Attribute\(\s*name\s*=\s*['\"](\w+)['\"]")
_CDK_SK = re.compile(r"sort_key\s*=\s*\w+\.Attribute\(\s*name\s*=\s*['\"](\w+)['\"]")
_CDK_TABLE_NAME = re.compile(r"\btable_name\s*=\s*['\"]([\w.-]+)['\"]")
_CDK_ADD_ENV = re.compile(r"add_environment\(\s*['\"](\w+)['\"]\s*,\s*\w+\.table_name")
_CDK_ENV_DICT = re.compile(r"['\"](\w+)['\"]\s*:\s*\w+\.table_name")


def _cdk_projects(scan_root: Path) -> list[Path]:
    return [p.parent for p in _files(scan_root, names=("cdk.json",))]


def _parse_cdk(project: Path) -> dict:
    assets, handlers, tables, env_names, srcs = [], [], [], [], {}
    for f in _files(project, (".py",)):
        rel_in = f.relative_to(project).as_posix()
        if rel_in.startswith(("tests/", "test/")) or "/tests/" in rel_in:
            continue
        text = _read(f)
        for m in _CDK_ASSET.finditer(text):
            assets.append(m.group(1))
            srcs.setdefault("build_dir", (f, _line(text, m.start())))
        for m in _CDK_HANDLER.finditer(text):
            handlers.append(m.group(1))
            srcs.setdefault("lambda_handler", (f, _line(text, m.start())))
        for m in _CDK_TABLE.finditer(text):
            body = text[m.end(): m.end() + 1500]
            pk, sk, tn = _CDK_PK.search(body), _CDK_SK.search(body), _CDK_TABLE_NAME.search(body)
            tables.append({"name": tn.group(1) if tn else "", "hash_key": pk.group(1) if pk else "",
                           "range_key": sk.group(1) if sk else ""})
            srcs.setdefault("dynamodb_tables", (f, _line(text, m.start())))
        env_names += _CDK_ADD_ENV.findall(text) + _CDK_ENV_DICT.findall(text)
    return {"assets": assets, "handlers": handlers, "tables": tables, "table_env": env_names, "sources": srcs}


# ── Python app code ─────────────────────────────────────────────────────────

_ROUTE = re.compile(r"@(\w+)\.(route|get|api_route)\(\s*['\"]([^'\"]*)['\"]([^)]*)\)")
_WEB_APP = re.compile(r"^(\w+)\s*=\s*(Flask|FastAPI|Quart|Starlette)\(", re.M)
_HANDLER_DEF = re.compile(r"^def\s+(lambda_handler|handler)\s*\(\s*event\s*,\s*context", re.M)
_ENV_GET = re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"](\w+)['\"]\s*(?:,\s*['\"]?([\w.:/-]*)['\"]?)?\s*\)")
_ENV_IDX = re.compile(r"os\.environ\[\s*['\"](\w+)['\"]\s*\]")
_ASSIGN_ENV = re.compile(
    r"^\s*(\w+)\s*=\s*os\.(?:environ\.get|getenv)\(\s*['\"](\w+)['\"]\s*(?:,\s*['\"]([\w.-]+)['\"])?\s*\)", re.M)
_ASSIGN_ENV_IDX = re.compile(r"^\s*(\w+)\s*=\s*os\.environ\[\s*['\"](\w+)['\"]\s*\]", re.M)
_ASSIGN_LIT = re.compile(r"^\s*(\w+)\s*=\s*['\"]([\w.-]+)['\"]\s*$", re.M)
_TABLE_ARG = re.compile(r"(?:TableName\s*=|\.Table\()\s*")
_KEY_ARG = re.compile(r"\bKey\s*=\s*\{\s*['\"](\w+)['\"]")
_APP_RUN_PORT = re.compile(r"\.run\([^)]*\bport\s*=\s*(\d{2,5})")
_PORT_ENV = re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"]PORT['\"]\s*,\s*['\"]?(\d{2,5})['\"]?\s*\)")
_UVICORN_RUN = re.compile(r"uvicorn\.run\([^)]*\bport\s*=\s*(\d{2,5})")


def _python_scan(repo: Path, app_root: Path) -> dict:
    out: dict = {"routes": [], "env_vars": set(), "tables": [], "handlers": [], "sources": {}}
    for f in _files(app_root, (".py",)):
        rel = _rel(repo, f)
        if _is_test_path(rel):
            continue
        text = _read(f)
        if not text:
            continue
        m = _WEB_APP.search(text)
        if m and "web_app" not in out:
            out["web_app"] = {"module": rel, "var": m.group(1)}
            out["framework"] = m.group(2).lower()
        for r in _ROUTE.finditer(text):
            path, rest, kind = r.group(3), r.group(4), r.group(2)
            if kind == "route" and "methods" in rest and "GET" not in rest.upper():
                continue
            out["routes"].append((path, rel, _line(text, r.start())))
        h = _HANDLER_DEF.search(text)
        if h:
            module = f.relative_to(app_root).with_suffix("").as_posix().replace("/", ".")
            out["handlers"].append((f"{module}.{h.group(1)}", rel, _line(text, h.start())))
        out["env_vars"].update(n for n, _ in _ENV_GET.findall(text))
        out["env_vars"].update(_ENV_IDX.findall(text))
        for pat in (_APP_RUN_PORT, _UVICORN_RUN, _PORT_ENV):
            pm = pat.search(text)
            if pm and "port" not in out:
                out["port"] = (int(pm.group(1)), rel, _line(text, pm.start()))
        # DynamoDB: variables bound to env vars / literals, then used as a table name.
        bound = {}
        for v, env, default in _ASSIGN_ENV.findall(text):
            bound[v] = {"env_var": env, "name": default}
        for v, env in _ASSIGN_ENV_IDX.findall(text):
            bound[v] = {"env_var": env, "name": ""}
        for v, lit in _ASSIGN_LIT.findall(text):
            bound.setdefault(v, {"env_var": "", "name": lit})
        for t in _TABLE_ARG.finditer(text):
            rest = text[t.end(): t.end() + 200]
            rec: dict = {}
            lit = re.match(r"['\"]([\w.-]+)['\"]", rest)
            env_get, env_idx = _ENV_GET.match(rest), _ENV_IDX.match(rest)
            ident = re.match(r"(\w+)\s*[,)\n]", rest)
            if lit:
                rec = {"name": lit.group(1), "env_var": ""}
            elif env_get:
                rec = {"env_var": env_get.group(1), "name": env_get.group(2) or ""}
            elif env_idx:
                rec = {"env_var": env_idx.group(1), "name": ""}
            elif ident and ident.group(1) in bound:
                rec = dict(bound[ident.group(1)])
            if rec:
                out["tables"].append({**rec, "_src": (rel, _line(text, t.start()))})
        for k in _KEY_ARG.finditer(text):
            out["tables"].append({"hash_key": k.group(1), "_src": (rel, _line(text, k.start()))})
    return out


def _dockerfile_facts(path: Path) -> dict:
    text = _read(path)
    facts: dict = {}
    m = re.search(r"^\s*EXPOSE\s+(\d{2,5})", text, re.M | re.I)
    if m:
        facts["port"] = (int(m.group(1)), _line(text, m.start()))
    else:
        c = re.search(r"(?:--port[ =]|--bind[ =][\w.]*:|-b\s+[\w.]*:)(\d{2,5})", text)
        if c:
            facts["port"] = (int(c.group(1)), _line(text, c.start()))
    froms = re.findall(r"^\s*FROM\s+\S*python:(\d+\.\d+)", text, re.M | re.I)
    if froms:
        facts["python"] = froms[-1]
    return facts


def _merge_tables(parts: list[dict]) -> list[dict]:
    """One record per table. With at most one named table, all hints describe it."""
    clean = [{k: v for k, v in p.items() if not k.startswith("_") and v} for p in parts]
    clean = [c for c in clean if c]
    if not clean:
        return []
    names = {c["name"] for c in clean if c.get("name")}
    if len(names) <= 1:
        merged: dict = {}
        for c in clean:
            for k in ("name", "hash_key", "range_key", "env_var"):
                if c.get(k) and not merged.get(k):
                    merged[k] = c[k]
        return [merged]
    by_name: dict[str, dict] = {}
    for c in clean:
        if c.get("name"):
            rec = by_name.setdefault(c["name"], {"name": c["name"]})
            for k in ("hash_key", "range_key", "env_var"):
                if c.get(k) and not rec.get(k):
                    rec[k] = c[k]
    return list(by_name.values())


def _language(app_root: Path) -> str:
    if any((app_root / n).exists() for n in ("pom.xml", "build.gradle", "build.gradle.kts")):
        return "java"
    if (app_root / "package.json").exists() and not _files(app_root, (".py",))[:1]:
        return "javascript"
    if _files(app_root, (".py",))[:1] or (app_root / "requirements.txt").exists():
        return "python"
    return ""


def _shallowest(paths: list[Path], repo: Path) -> list[Path]:
    if not paths:
        return []
    depth = min(len(p.relative_to(repo).parts) for p in paths)
    return [p for p in paths if len(p.relative_to(repo).parts) == depth]


# ── public ──────────────────────────────────────────────────────────────────

def _spring_props(app_root: Path) -> tuple[dict[str, tuple[str, Path, int]], Path | None]:
    """Flattened application.properties / application.yml keys → (value, file, line)."""
    res = app_root / "src" / "main" / "resources"
    out: dict[str, tuple[str, Path, int]] = {}
    found = None
    props = res / "application.properties"
    if props.is_file():
        found = props
        for i, line in enumerate(_read(props).splitlines(), 1):
            m = re.match(r"\s*([\w.\-\[\]]+)\s*[=:]\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith(("#", "!")):
                out[m.group(1)] = (m.group(2), props, i)
    for name in ("application.yml", "application.yaml"):
        y = res / name
        if not y.is_file():
            continue
        found = found or y
        stack: list[tuple[int, str]] = []
        for i, line in enumerate(_read(y).splitlines(), 1):
            m = re.match(r"^(\s*)([\w.\-]+):\s*(.*?)\s*$", line)
            if not m or line.lstrip().startswith("#"):
                continue
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            key = ".".join([k for _, k in stack] + [m.group(2)])
            if m.group(3) and not m.group(3).startswith("#"):
                out.setdefault(key, (m.group(3).strip("'\""), y, i))
            stack.append((indent, m.group(2)))
    return out, found


def _spring_actuator_health(app_root: Path) -> tuple[str, Path, int] | None:
    """Spring Boot actuator health path from the build file + application config (Boot 1.x:
    management.contextPath; 2+: management.endpoints.web.base-path, default /actuator), behind the
    server context path. APP-TE 2026-10-27: PetClinic serves /manage/health — /health was a 404."""
    build = next((app_root / n for n in ("pom.xml", "build.gradle", "build.gradle.kts") if (app_root / n).is_file()), None)
    text = _read(build) if build else ""
    if "spring-boot-starter-actuator" not in text:
        return None
    m = re.search(r"spring-boot-starter-parent</artifactId>\s*<version>(\d+)", text) or \
        re.search(r"org\.springframework\.boot['\"]?\)?\s*version\s*['\"](\d+)", text)
    major = int(m.group(1)) if m else 2
    props, cfg = _spring_props(app_root)

    def get(*keys: str) -> tuple[str, Path, int] | None:
        return next((props[k] for k in keys if k in props), None)

    server = get("server.servlet.context-path", "server.servlet.contextPath", "server.context-path", "server.contextPath")
    if major < 2:
        mgmt = get("management.contextPath", "management.context-path")
        base = mgmt[0] if mgmt else ""
    else:
        mgmt = get("management.endpoints.web.base-path", "management.endpoints.web.basePath")
        base = mgmt[0] if mgmt else "/actuator"
    path = "/" + "/".join(x.strip("/") for x in ((server[0] if server else ""), base, "health") if x.strip("/"))
    where = mgmt or server
    return path, (where[1] if where else (cfg or build)), (where[2] if where else 0)


def scan_wiring(repo_dir: Path, build_dir: str) -> dict:
    """Detected runtime wiring for the app in `repo_dir` (see module docstring).

    `build_dir` is the human/recorded app folder ('' = unknown/root). When it is a
    CDK or SAM project that ships one function asset, the asset folder is returned
    as the detected `build_dir`; with no build_dir, the shallowest folder holding
    the app (IaC asset, Dockerfile, requirements.txt) is used when unambiguous.
    """
    repo = Path(repo_dir)
    given = _norm(build_dir)
    scan_root = repo / given if given and (repo / given).is_dir() else repo
    result: dict = {}
    sources: dict[str, str] = {}

    def src(field: str, path: Path | str, line: int | None = None):
        rel = path if isinstance(path, str) else _rel(repo, path)
        sources.setdefault(field, f"{rel}:{line}" if line else rel)

    # IaC: SAM templates and CDK projects under the scan root.
    sam = []
    for t in _sam_templates(scan_root):
        if t.name.startswith("serverless."):
            hs = _serverless_handlers(t)
            if hs:
                sam.append({"path": t, "functions": [{"name": h, "handler": h} for h in hs], "tables": [],
                            "code_uris": []})
            continue
        parsed = _parse_sam(t)
        if parsed.get("functions") or parsed.get("tables"):
            sam.append({"path": t, **parsed})
    cdk = [(p, _parse_cdk(p)) for p in _cdk_projects(scan_root)]
    cdk = [(p, c) for p, c in cdk if c["assets"] or c["handlers"] or c["tables"]]
    if not sam and not cdk and scan_root != repo:
        # build_dir is already the function folder: its IaC lives in an ancestor
        # project (CDK cdk.json / SAM template) whose asset points back here.
        for anc in list(scan_root.parents):
            if anc == repo.parent:
                break
            if (anc / "cdk.json").is_file():
                c = _parse_cdk(anc)
                if any((anc / a).resolve() == scan_root.resolve() for a in c["assets"]):
                    cdk = [(anc, c)]
                    break
            tpl = next((t for t in sorted(anc.glob("template*.y*ml"))), None)
            if tpl:
                parsed = _parse_sam(tpl)
                if any((anc / u).resolve() == scan_root.resolve() for u in parsed.get("code_uris", [])):
                    sam = [{"path": tpl, **parsed}]
                    break
            if anc == repo:
                break

    # Where the app's code lives.
    app_root = scan_root
    asset_dirs: list[Path] = []
    for p, c in cdk:
        asset_dirs += [(p / a).resolve() for a in c["assets"] if (p / a).is_dir()]
    for s in sam:
        asset_dirs += [(s["path"].parent / u).resolve() for u in s.get("code_uris", [])
                       if (s["path"].parent / u).is_dir()]
    asset_dirs = list(dict.fromkeys(asset_dirs))
    if len(asset_dirs) == 1:
        app_root = asset_dirs[0]
        for p, c in cdk:
            if c["sources"].get("build_dir"):
                f, ln = c["sources"]["build_dir"]
                src("build_dir", f, ln)
    elif not given and not asset_dirs:
        markers = _files(repo, names=("Dockerfile", "requirements.txt", "pyproject.toml", "pom.xml"))
        roots = list(dict.fromkeys(p.parent for p in markers))
        shallow = _shallowest(roots, repo)
        if len(shallow) == 1:
            app_root = shallow[0]
    try:
        detected_bd = app_root.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        detected_bd = given
    detected_bd = "" if detected_bd == "." else detected_bd
    if detected_bd != given or given:
        result["build_dir"] = detected_bd or "."
        if detected_bd != given:
            sources.setdefault("build_dir", detected_bd or ".")

    lang = _language(app_root) or _language(scan_root)
    if lang:
        result["language"] = lang

    # Dockerfile: the app folder's, else the repo root's.
    docker = next((d / "Dockerfile" for d in (app_root, scan_root, repo) if (d / "Dockerfile").is_file()), None)
    dfacts = _dockerfile_facts(docker) if docker else {}

    py = _python_scan(repo, app_root) if lang == "python" else {"routes": [], "env_vars": set(), "tables": [],
                                                                 "handlers": [], "sources": {}}

    # runtime version
    rv = dfacts.get("python")
    if rv:
        src("runtime_version", docker)
    for s in sam:
        for fn in s.get("functions", []):
            m = re.match(r"python(\d+\.\d+)", fn.get("runtime", ""))
            if m and not rv:
                rv = m.group(1)
                src("runtime_version", s["path"])
    for p, _c in cdk:
        for f in _files(p, (".py",)):
            m = re.search(r"Runtime\.PYTHON_(\d+)_(\d+)", _read(f))
            if m and not rv:
                rv = f"{m.group(1)}.{m.group(2)}"
                src("runtime_version", f)
    if rv:
        result["runtime_version"] = rv

    # port
    if "port" in dfacts:
        result["container_port"] = dfacts["port"][0]
        src("container_port", docker, dfacts["port"][1])
    elif "port" in py:
        result["container_port"] = py["port"][0]
        src("container_port", py["port"][1], py["port"][2])

    # health path
    routes = [r for r in py["routes"] if "<" not in r[0] and "{" not in r[0]]
    if routes:
        pick = next((r for pref in _HEALTH_PREFERENCE for r in routes if r[0].rstrip("/") == pref.rstrip("/")), routes[0])
        result["health_check_path"] = pick[0] or "/"
        src("health_check_path", pick[1], pick[2])
    if "health_check_path" not in result and lang == "java":
        spring = _spring_actuator_health(app_root)
        if spring:
            result["health_check_path"] = spring[0]
            src("health_check_path", spring[1], spring[2])
    if py.get("framework"):
        result["framework"] = py["framework"]
        result["web_app"] = py["web_app"]

    # Lambda handler + functions
    functions = [fn for s in sam for fn in s.get("functions", [])]
    if functions:
        result["lambda_functions"] = functions
        src("lambda_functions", sam[0]["path"])
    handler = ""
    if functions and functions[0].get("handler"):
        handler = functions[0]["handler"]
        src("lambda_handler", sam[0]["path"])
    if not handler:
        for _p, c in cdk:
            if c["handlers"]:
                handler = c["handlers"][0]
                f, ln = c["sources"]["lambda_handler"]
                src("lambda_handler", f, ln)
                break
    if not handler and py["handlers"] and not py.get("framework"):
        handler = py["handlers"][0][0]
        src("lambda_handler", py["handlers"][0][1], py["handlers"][0][2])
    if handler:
        result["lambda_handler"] = handler

    # DynamoDB tables: IaC first (authoritative key schema), then code hints.
    parts: list[dict] = []
    for s in sam:
        for t in s.get("tables", []):
            parts.append({k: v for k, v in t.items() if k != "logical_id"})
            src("dynamodb_tables", s["path"])
    for _p, c in cdk:
        for i, t in enumerate(c["tables"]):
            env = c["table_env"][i] if i < len(c["table_env"]) else (c["table_env"][0] if c["table_env"] else "")
            parts.append({**t, "env_var": env})
        if c["tables"]:
            f, ln = c["sources"]["dynamodb_tables"]
            src("dynamodb_tables", f, ln)
    for t in py["tables"]:
        if t.get("_src"):
            src("dynamodb_tables", t["_src"][0], t["_src"][1])
        parts.append(t)
    tables = _merge_tables(parts)
    if tables:
        result["dynamodb_tables"] = tables

    # Spring profiles the repo ships (application-<p>.properties/.yml): tool_execution activates the
    # one matching an RDS target engine (PetClinic: application-mysql.properties).
    if lang == "java":
        res = app_root / "src" / "main" / "resources"
        profiles = sorted({m.group(1) for f in (res.iterdir() if res.is_dir() else [])
                           if (m := re.fullmatch(r"application-([\w-]+)\.(?:properties|ya?ml)", f.name))})
        if profiles:
            result["spring_profiles"] = profiles
            src("spring_profiles", res)

    env_vars = sorted(py["env_vars"] | {e for fn in functions for e in (fn.get("env") or {})})
    if env_vars:
        result["env_vars"] = env_vars
    if sources:
        result["sources"] = sources
    return result


def third_party_imports(app_root: Path, include_tests: bool = False) -> set[str]:
    """Top-level imported module names that aren't stdlib or local to app_root."""
    import sys

    local = {p.stem for p in _files(app_root, (".py",))}
    local |= {p.name for p in app_root.rglob("*") if p.is_dir() and p.name not in _SKIP_DIRS}
    names: set[str] = set()
    for f in _files(app_root, (".py",)):
        if not include_tests and _is_test_path(_rel(app_root, f)):
            continue
        try:
            tree = ast.parse(_read(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return {n for n in names if n not in sys.stdlib_module_names and n not in local and n != "__future__"}
