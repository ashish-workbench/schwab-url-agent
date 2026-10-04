"""Template-driven code generation used by the implement agents.

Templates are real Python files with line-level feature markers:

    # @if <flag>   /   # @else   /   # @endif

and ``{{NAME}}`` value placeholders. Rendering is deterministic, which keeps output hashes
stable so re-planning can tell "upstream changed" from "upstream re-ran with the same result".
An LLM-backed agent can replace this module behind the same agent interface.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"

APP_FILES = ("app/__init__.py", "app/store.py", "app/main.py")
TEST_FILES = ("tests/conftest.py", "tests/test_store.py", "tests/test_api.py")
FALLBACK_TEST_FILES = ("tests/conftest.py", "tests/test_store.py")
DOC_FILES = ("docs/API.md", "CHANGELOG.md")

# Requirement traceability: acceptance criterion -> tests that prove it.
AC_TESTS: dict[str, tuple[str, ...]] = {
    "AC1": ("test_create_redirect_and_count", "test_create_and_click"),
    "AC2": ("test_create_redirect_and_count",),
    "AC3": ("test_create_redirect_and_count", "test_unknown_code_returns_404"),
    "AC4": ("test_rejects_unsafe_urls", "test_rejects_bad_urls", "test_rejects_oversized_url"),
    "AC5": ("test_expired_link_returns_410", "test_expired_link_is_not_followed", "test_rejects_invalid_ttl"),
    "AC6": ("test_default_ttl_applied",),
    "AC7": ("test_migration_keeps_existing_links",),
    "AC8": ("test_stats_expose_no_pii",),
}

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def flags_for(spec: dict) -> dict[str, bool]:
    expiry = bool(spec.get("expiry"))
    default_ttl = expiry and spec.get("default_ttl_seconds") is not None
    return {"expiry": expiry, "default_ttl": default_ttl, "optional_ttl": expiry and not default_ttl}


def values_for(spec: dict) -> dict[str, str]:
    return {
        "MAX_URL_LENGTH": str(spec.get("max_url_length", 2048)),
        "MAX_TTL_SECONDS": str(spec.get("max_ttl_seconds", 365 * 24 * 3600)),
        "DEFAULT_TTL_SECONDS": str(spec.get("default_ttl_seconds") or 0),
    }


def render_text(text: str, flags: dict[str, bool], values: dict[str, str]) -> str:
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []  # (parent_active, branch_active)
    for line in text.replace("\r\n", "\n").split("\n"):
        marker = line.strip()
        active = all(b for _, b in stack)
        if marker.startswith("# @if "):
            flag = marker[len("# @if "):].strip()
            if flag not in flags:
                raise KeyError(f"unknown template flag: {flag}")
            stack.append((active, bool(flags[flag])))
            continue
        if marker == "# @else":
            parent, branch = stack.pop()
            stack.append((parent, not branch))
            continue
        if marker == "# @endif":
            stack.pop()
            continue
        if active:
            out.append(line)
    if stack:
        raise ValueError("unbalanced template markers")

    def sub(match: re.Match) -> str:
        return values[match.group(1)]

    return _PLACEHOLDER.sub(sub, "\n".join(out))


def render(spec: dict, files: tuple[str, ...]) -> dict[str, str]:
    flags, values = flags_for(spec), values_for(spec)
    rendered = {}
    for rel in files:
        template = (TEMPLATE_ROOT / f"{rel}.tmpl").read_text(encoding="utf-8")
        rendered[rel] = render_text(template, flags, values)
    return rendered


def write_files(root: Path, files: dict[str, str]) -> list[str]:
    """Write files, returning only those whose content actually changed."""
    changed = []
    for rel, content in files.items():
        target = root / rel
        old = target.read_text(encoding="utf-8").replace("\r\n", "\n") if target.exists() else None
        if old == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        changed.append(rel)
    return sorted(changed)


def acceptance_tests(criteria: list[str]) -> dict[str, tuple[str, ...]]:
    return {ac: AC_TESTS.get(ac, ()) for ac in criteria}
