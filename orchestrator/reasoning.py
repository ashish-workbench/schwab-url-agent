"""Codebase reasoning for brownfield work.

Parses the existing workspace with ``ast`` (no regex guessing on code structure) to find
modules, functions, HTTP routes, call edges and SQL tables, then derives which of those a
requested feature touches. The design agent uses this to name impacted modules *before*
anything is edited, and the change-control policy uses the result as the allowed blast radius.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_CREATE_TABLE = re.compile(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\)\s*$", re.S | re.I | re.M)
_SQL_VERBS = re.compile(r"\b(INSERT INTO|UPDATE|SELECT .*? FROM|DELETE FROM)\s+(\w+)", re.I)


def index_codebase(root: Path) -> dict:
    """Return a JSON-serialisable index of python modules under ``root/app`` and ``root/tests``."""
    index: dict = {"modules": {}, "routes": [], "tables": {}}
    for sub in ("app", "tests"):
        base = root / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            index["modules"][rel] = _module_info(tree, rel, index)
    return index


def _module_info(tree: ast.Module, rel: str, index: dict) -> dict:
    functions: dict[str, dict] = {}
    for node in ast.walk(tree) if rel.startswith("app/") else ():
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for table, body in _CREATE_TABLE.findall(node.value):
                cols = [c.strip().split()[0] for c in body.split(",") if c.strip()]
                index["tables"][table] = {"file": rel, "columns": cols}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = sorted(
            {
                (n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id)
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
            }
        )
        sql = sorted(
            {
                f"{verb.split()[0].upper()} {table}"
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                for verb, table in _SQL_VERBS.findall(n.value)
            }
        )
        functions[node.name] = {"line": node.lineno, "calls": calls, "sql": sql}
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in {"get", "post", "put", "delete", "patch"}
                and deco.args
                and isinstance(deco.args[0], ast.Constant)
            ):
                index["routes"].append(
                    {"method": deco.func.attr.upper(), "path": deco.args[0].value, "handler": node.name, "file": rel}
                )
    return {"functions": functions}


def detect_features(index: dict) -> set[str]:
    """Infer which product features the existing code already implements."""
    present: set[str] = set()
    routes = {(r["method"], r["path"]) for r in index.get("routes", [])}
    if ("POST", "/links") in routes:
        present.add("create")
    if any(m == "GET" and p.startswith("/{") for m, p in routes):
        present.add("redirect")
    if any("stats" in p for _, p in routes):
        present.add("analytics")
    if "expires_at" in index.get("tables", {}).get("links", {}).get("columns", []):
        present.add("expiry")
    return present


# Which code concerns each feature touches. Resolved against the real index below.
_FEATURE_CONCERNS = {
    "expiry": {
        "table": ("links", "add nullable column expires_at"),
        "sql_writers": ("INSERT links", "accept ttl, persist expires_at"),
        "sql_click": ("UPDATE links", "refuse clicks after expiry (atomic predicate)"),
        "routes": {
            ("POST", "/links"): "accept optional ttl_seconds, return expires_at",
            ("GET", "/{code}"): "return 410 Gone for expired links",
            ("GET", "/links/{code}/stats"): "expose expires_at / expired",
        },
    },
}


def impact_analysis(index: dict, delta: set[str]) -> list[dict]:
    impact: list[dict] = []
    for feature in sorted(delta):
        concern = _FEATURE_CONCERNS.get(feature)
        if not concern:
            continue
        table, why = concern["table"]
        if table in index["tables"]:
            impact.append({"feature": feature, "kind": "table", "name": table, "file": index["tables"][table]["file"], "change": why})
        touched_funcs: set[str] = set()
        for key in ("sql_writers", "sql_click"):
            needle, why = concern[key]
            for rel, mod in index["modules"].items():
                for fname, info in mod["functions"].items():
                    if needle in info["sql"]:
                        touched_funcs.add(fname)
                        impact.append({"feature": feature, "kind": "function", "name": fname, "file": rel, "line": info["line"], "change": why})
        for route in index["routes"]:
            why = concern["routes"].get((route["method"], route["path"]))
            if why:
                impact.append({"feature": feature, "kind": "route", "name": f"{route['method']} {route['path']}", "file": route["file"], "handler": route["handler"], "change": why})
        for rel, mod in index["modules"].items():
            if not rel.startswith("tests/"):
                continue
            users = sorted({f for f, info in mod["functions"].items() if touched_funcs & set(info["calls"]) or "post" in info["calls"]})
            if users:
                impact.append({"feature": feature, "kind": "tests", "name": rel, "file": rel, "change": f"extend coverage; {len(users)} existing tests exercise impacted code"})
    return impact


def data_flows(index: dict) -> list[str]:
    flows = []
    for route in index.get("routes", []):
        mod = index["modules"].get(route["file"], {})
        calls = mod.get("functions", {}).get(route["handler"], {}).get("calls", [])
        store_calls = [c for c in calls if c in {"create_link", "record_click", "get_link", "is_expired"}]
        if store_calls:
            flows.append(f"{route['method']} {route['path']} -> {route['handler']} -> {' -> '.join(store_calls)} -> links")
    return flows
