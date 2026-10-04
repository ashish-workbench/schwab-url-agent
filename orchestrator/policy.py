"""Policy guardrails: security, compliance and change control.

Each rule returns ``Violation`` records. ``block`` violations stop the run immediately (no
retry: a deterministic policy failure will not fix itself) and trigger rollback.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    rule: str
    category: str
    severity: str  # "block" | "warn"
    file: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


RULES = {
    "SEC-001": ("security", "No secrets or credentials in generated artifacts"),
    "SEC-002": ("security", "No dynamic code execution or shell injection primitives"),
    "SEC-003": ("security", "Redirect targets must pass the URL allow-list before persistence"),
    "CMP-001": ("compliance", "No PII columns in analytics storage"),
    "CHG-001": ("change-control", "Changes stay inside the design-approved file set"),
    "CHG-002": ("change-control", "No destructive schema or data operations"),
}

_SECRETS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{6,}['\"]"),
]
_DANGEROUS = {"eval", "exec", "system", "popen", "loads"}  # loads only flagged for pickle/marshal
_PII_COLUMNS = {"ip", "ip_address", "user_agent", "email", "phone", "ssn", "account_number", "dob"}
_DESTRUCTIVE = re.compile(r"(?i)\b(DROP\s+(TABLE|COLUMN)|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\w+\s*(['\";]|$))")
_CREATE_TABLE = re.compile(r"(?is)CREATE TABLE(?: IF NOT EXISTS)?\s+\w+\s*\((.*?)\)\s*$", re.M)


def _iter_files(root: Path, files: list[str] | None) -> list[tuple[str, str]]:
    if files is None:
        files = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        ]
    out = []
    for rel in sorted(files):
        path = root / rel
        if path.exists() and path.suffix in {".py", ".md", ".txt", ".json", ".toml", ".cfg", ".ini", ".env", ""}:
            out.append((rel, path.read_text(encoding="utf-8", errors="replace")))
    return out


def scan(root: Path, files: list[str] | None = None, allowed: list[str] | None = None) -> list[Violation]:
    found: list[Violation] = []
    for rel, text in _iter_files(root, files):
        for pattern in _SECRETS:
            if pattern.search(text):
                found.append(Violation("SEC-001", "security", "block", rel, f"matches {pattern.pattern[:40]}"))
                break
        if rel.endswith(".py"):
            found.extend(_python_rules(rel, text))
        if rel.startswith("app/") and _DESTRUCTIVE.search(text):
            found.append(Violation("CHG-002", "change-control", "block", rel, "destructive SQL statement"))
        if rel.startswith("app/"):
            for body in _CREATE_TABLE.findall(text):
                cols = {c.strip().split()[0].lower() for c in body.split(",") if c.strip()}
                pii = sorted(cols & _PII_COLUMNS)
                if pii:
                    found.append(Violation("CMP-001", "compliance", "block", rel, f"PII columns {pii}"))
    if allowed is not None:
        for rel in sorted(set(files or []) - set(allowed)):
            found.append(Violation("CHG-001", "change-control", "block", rel, "file outside the approved design scope"))
    store = root / "app" / "store.py"
    if store.exists() and (files is None or "app/store.py" in (files or [])):
        found.extend(_redirect_guard(store))
    return found


def _python_rules(rel: str, text: str) -> list[Violation]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [Violation("SEC-002", "security", "block", rel, f"unparseable python: {exc.msg}")]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        owner = getattr(getattr(func, "value", None), "id", "")
        if name in {"eval", "exec"} or (name in {"system", "popen"} and owner == "os"):
            out.append(Violation("SEC-002", "security", "block", rel, f"call to {owner + '.' if owner else ''}{name} at line {node.lineno}"))
        if name == "loads" and owner in {"pickle", "marshal"}:
            out.append(Violation("SEC-002", "security", "block", rel, f"unsafe deserialisation at line {node.lineno}"))
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                out.append(Violation("SEC-002", "security", "block", rel, f"shell=True at line {node.lineno}"))
    return out


def _redirect_guard(store: Path) -> list[Violation]:
    tree = ast.parse(store.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    create = funcs.get("create_link")
    if "validate_url" not in funcs or create is None:
        return [Violation("SEC-003", "security", "block", "app/store.py", "validate_url/create_link missing")]
    called = {getattr(n.func, "id", None) for n in ast.walk(create) if isinstance(n, ast.Call)}
    if "validate_url" not in called:
        return [Violation("SEC-003", "security", "block", "app/store.py", "create_link persists URLs without validate_url")]
    return []
